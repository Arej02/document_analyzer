<?php
/**
 * PHP-side of the WordPressExtractor subprocess boundary (tasks.md Phase 2,
 * design.md §5 / skills.md "custom WordPress extractor"). Invoked by
 * document_analyzer.worker.wordpress_extractor as:
 *
 *   php wp_extract.php <repo_path>
 *
 * Separate script from extract.php (PhpExtractor) by design — each Extractor
 * implementation is self-contained per design.md §5 ("New language support =
 * new class implementing this protocol, nothing else in the pipeline
 * changes"). This one does not build a general call graph; it looks only for
 * WordPress-specific signal calls anywhere in a file's AST (top-level or
 * inside any function/method) and reports their literal arguments:
 *
 *   - add_action / add_filter        -> hook registrations (+ callback)
 *   - do_action / apply_filters      -> hook invocations
 *     (and their *_ref_array variants)
 *   - register_post_type             -> CPTs
 *   - register_post_meta / register_meta -> meta registrations
 *
 * Arguments are evaluated only when they're literal PHP values (strings,
 * numbers, bools, null, or arrays of the same); anything computed at runtime
 * (a variable, a function call, a constant other than true/false/null) comes
 * back as `null` with nothing to indicate the field itself, matching the
 * static-parsing-only rule (CLAUDE.md, design.md §1) — this is static
 * analysis, not execution.
 */

declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;

/** Evaluates an expression to a literal PHP value, if it is one. */
function literalValue(Node $expr): array
{
    if ($expr instanceof Node\Scalar\String_) {
        return ['literal' => true, 'value' => $expr->value];
    }
    if (($expr instanceof Node\Scalar\LNumber) || (class_exists(Node\Scalar\Int_::class) && $expr instanceof Node\Scalar\Int_)) {
        return ['literal' => true, 'value' => $expr->value];
    }
    if (($expr instanceof Node\Scalar\DNumber) || (class_exists(Node\Scalar\Float_::class) && $expr instanceof Node\Scalar\Float_)) {
        return ['literal' => true, 'value' => $expr->value];
    }
    if ($expr instanceof Expr\ConstFetch) {
        $name = strtolower($expr->name->toString());
        if ($name === 'true') {
            return ['literal' => true, 'value' => true];
        }
        if ($name === 'false') {
            return ['literal' => true, 'value' => false];
        }
        if ($name === 'null') {
            return ['literal' => true, 'value' => null];
        }
        return ['literal' => false, 'value' => null];
    }
    if ($expr instanceof Expr\UnaryMinus) {
        $inner = literalValue($expr->expr);
        if ($inner['literal'] && is_numeric($inner['value'])) {
            return ['literal' => true, 'value' => -$inner['value']];
        }
        return ['literal' => false, 'value' => null];
    }
    if ($expr instanceof Expr\Array_) {
        $isList = true;
        $assoc = [];
        $list = [];
        foreach ($expr->items as $item) {
            if ($item === null) {
                continue;
            }
            $value = literalValue($item->value);
            $resolved = $value['literal'] ? $value['value'] : null;
            if ($item->key !== null) {
                $isList = false;
                $key = literalValue($item->key);
                $assoc[$key['literal'] ? (string) $key['value'] : '?'] = $resolved;
            } else {
                $list[] = $resolved;
            }
        }
        return ['literal' => true, 'value' => $isList ? $list : $assoc];
    }
    return ['literal' => false, 'value' => null];
}

function literalOrNull(array $args, int $index): mixed
{
    if (!isset($args[$index])) {
        return null;
    }
    $result = literalValue($args[$index]->value);
    return $result['literal'] ? $result['value'] : null;
}

/** Best-effort description of an add_action/add_filter callback argument. */
function describeCallback(Node\Arg $arg): array
{
    $expr = $arg->value;

    if ($expr instanceof Node\Scalar\String_) {
        return ['name' => $expr->value, 'kind' => 'function'];
    }

    if ($expr instanceof Expr\Array_ && count($expr->items) === 2 && $expr->items[0] !== null && $expr->items[1] !== null) {
        $methodPart = literalValue($expr->items[1]->value);
        $methodName = $methodPart['literal'] ? $methodPart['value'] : null;
        if (!is_string($methodName)) {
            return ['name' => null, 'kind' => 'unknown'];
        }

        $target = $expr->items[0]->value;
        if ($target instanceof Expr\Variable && $target->name === 'this') {
            return ['name' => "self::{$methodName}", 'kind' => 'method'];
        }
        if ($target instanceof Node\Scalar\String_) {
            return ['name' => "{$target->value}::{$methodName}", 'kind' => 'method'];
        }
        return ['name' => null, 'kind' => 'unknown'];
    }

    if ($expr instanceof Expr\Closure || $expr instanceof Expr\ArrowFunction) {
        return ['name' => null, 'kind' => 'closure'];
    }

    return ['name' => null, 'kind' => 'unknown'];
}

/** Collects WordPress-signal calls (hooks, CPTs, meta) anywhere in a file's AST. */
final class WpCallCollector extends NodeVisitorAbstract
{
    public array $hookRegistrations = [];
    public array $hookInvocations = [];
    public array $cpts = [];
    public array $metas = [];

    public function enterNode(Node $node)
    {
        if (!$node instanceof Expr\FuncCall || !$node->name instanceof Node\Name) {
            return null;
        }

        $fn = strtolower($node->name->toString());
        $args = $node->args;

        if ($fn === 'add_action' || $fn === 'add_filter') {
            $callback = isset($args[1]) ? describeCallback($args[1]) : ['name' => null, 'kind' => 'unknown'];
            $this->hookRegistrations[] = [
                'hook_type' => $fn === 'add_action' ? 'action' : 'filter',
                'hook_name' => literalOrNull($args, 0),
                'callback_name' => $callback['name'],
                'callback_kind' => $callback['kind'],
                'priority' => literalOrNull($args, 2),
                'accepted_args' => literalOrNull($args, 3),
                'line' => $node->getStartLine(),
            ];
        } elseif (in_array($fn, ['do_action', 'do_action_ref_array', 'apply_filters', 'apply_filters_ref_array'], true)) {
            $this->hookInvocations[] = [
                'hook_type' => str_starts_with($fn, 'do_action') ? 'action' : 'filter',
                'hook_name' => literalOrNull($args, 0),
                'line' => $node->getStartLine(),
            ];
        } elseif ($fn === 'register_post_type') {
            $this->cpts[] = [
                'slug' => literalOrNull($args, 0),
                'args' => literalOrNull($args, 1),
                'line' => $node->getStartLine(),
            ];
        } elseif ($fn === 'register_post_meta' || $fn === 'register_meta') {
            $this->metas[] = [
                'object_type' => literalOrNull($args, 0),
                'meta_key' => literalOrNull($args, 1),
                'args' => literalOrNull($args, 2),
                'line' => $node->getStartLine(),
            ];
        }

        return null;
    }
}

function extractFile(string $absolutePath, string $relativePath, $parser): array
{
    $code = file_get_contents($absolutePath);
    if ($code === false) {
        return [
            'file' => $relativePath, 'error' => 'could not read file',
            'hook_registrations' => [], 'hook_invocations' => [], 'cpts' => [], 'metas' => [],
        ];
    }

    try {
        $ast = $parser->parse($code);
    } catch (Error $e) {
        return [
            'file' => $relativePath, 'error' => $e->getMessage(),
            'hook_registrations' => [], 'hook_invocations' => [], 'cpts' => [], 'metas' => [],
        ];
    }

    $collector = new WpCallCollector();
    $traverser = new NodeTraverser();
    $traverser->addVisitor($collector);
    $traverser->traverse($ast ?? []);

    return [
        'file' => $relativePath,
        'error' => null,
        'hook_registrations' => $collector->hookRegistrations,
        'hook_invocations' => $collector->hookInvocations,
        'cpts' => $collector->cpts,
        'metas' => $collector->metas,
    ];
}

function main(array $argv): int
{
    if (count($argv) < 2) {
        fwrite(STDERR, "usage: php wp_extract.php <repo_path>\n");
        return 2;
    }

    $repoPath = rtrim($argv[1], "/\\");
    if (!is_dir($repoPath)) {
        fwrite(STDERR, "not a directory: {$repoPath}\n");
        return 2;
    }

    $parser = (new ParserFactory())->createForNewestSupportedVersion();

    $files = [];
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($repoPath, FilesystemIterator::SKIP_DOTS)
    );
    foreach ($iterator as $fileInfo) {
        if ($fileInfo->isFile() && strtolower($fileInfo->getExtension()) === 'php') {
            $files[] = $fileInfo->getPathname();
        }
    }
    sort($files);

    $results = [];
    foreach ($files as $absolutePath) {
        $relativePath = str_replace('\\', '/', substr($absolutePath, strlen($repoPath) + 1));
        $results[] = extractFile($absolutePath, $relativePath, $parser);
    }

    echo json_encode($results, JSON_UNESCAPED_SLASHES);
    return 0;
}

exit(main($argv));
