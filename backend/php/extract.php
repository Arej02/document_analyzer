<?php
/**
 * PHP-side of the PhpExtractor subprocess boundary (tasks.md Phase 2,
 * design.md §5 / skills.md "PHP parsing"). Invoked by
 * document_analyzer.worker.php_extractor as:
 *
 *   php extract.php <repo_path>
 *
 * Walks every *.php file under <repo_path>, parses it with nikic/php-parser,
 * and prints a single JSON document to stdout describing top-level functions
 * and classes (with their methods) found in each file — pure static parsing,
 * no execution of any parsed code. A file that fails to parse is reported
 * with an "error" field instead of aborting the whole run, so one bad file
 * doesn't take down extraction for the rest of the repo (CLAUDE.md:
 * "grounding checker failures never block ingestion" — same principle
 * applied one stage earlier).
 */

declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\Stmt;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;

/** Collects FuncCall/MethodCall/StaticCall names and include/require targets in a function body. */
final class CallCollector extends NodeVisitorAbstract
{
    /** @var string[] */
    public array $calls = [];
    /** @var string[] */
    public array $includes = [];

    public function enterNode(Node $node)
    {
        if ($node instanceof Expr\FuncCall && $node->name instanceof Node\Name) {
            $this->calls[] = $node->name->toString();
        } elseif ($node instanceof Expr\MethodCall && $node->name instanceof Node\Identifier) {
            $this->calls[] = $node->name->toString();
        } elseif ($node instanceof Expr\StaticCall && $node->name instanceof Node\Identifier) {
            $class = $node->class instanceof Node\Name ? $node->class->toString() . '::' : '';
            $this->calls[] = $class . $node->name->toString();
        } elseif ($node instanceof Expr\Include_) {
            $this->includes[] = $node->expr instanceof Node\Scalar\String_
                ? $node->expr->value
                : 'dynamic';
        }
        return null;
    }
}

function paramNames(array $params): array
{
    return array_map(
        static fn (Node\Param $p) => $p->var instanceof Node\Expr\Variable && is_string($p->var->name)
            ? $p->var->name
            : null,
        $params
    );
}

function collectCallsAndIncludes(array $bodyStmts): array
{
    $collector = new CallCollector();
    $traverser = new NodeTraverser();
    $traverser->addVisitor($collector);
    $traverser->traverse($bodyStmts);
    return [$collector->calls, $collector->includes];
}

function describeFunction(Stmt\Function_ $fn): array
{
    [$calls, $includes] = collectCallsAndIncludes($fn->stmts ?? []);
    return [
        'name' => $fn->name->toString(),
        'line' => $fn->getStartLine(),
        'params' => paramNames($fn->params),
        'calls' => $calls,
        'includes' => $includes,
    ];
}

function describeMethod(Stmt\ClassMethod $method): array
{
    [$calls, $includes] = collectCallsAndIncludes($method->stmts ?? []);
    return [
        'name' => $method->name->toString(),
        'line' => $method->getStartLine(),
        'params' => paramNames($method->params),
        'visibility' => $method->isPublic() ? 'public' : ($method->isProtected() ? 'protected' : 'private'),
        'static' => $method->isStatic(),
        'calls' => $calls,
        'includes' => $includes,
    ];
}

function describeClassLike(Stmt\ClassLike $class): array
{
    $methods = [];
    foreach ($class->getMethods() as $method) {
        $methods[] = describeMethod($method);
    }

    $extends = null;
    $implements = [];
    if ($class instanceof Stmt\Class_) {
        $extends = $class->extends?->toString();
        $implements = array_map(static fn (Node\Name $n) => $n->toString(), $class->implements);
    } elseif ($class instanceof Stmt\Interface_) {
        $implements = array_map(static fn (Node\Name $n) => $n->toString(), $class->extends);
    }

    return [
        'name' => $class->name?->toString() ?? '(anonymous)',
        'line' => $class->getStartLine(),
        'extends' => $extends,
        'implements' => $implements,
        'methods' => $methods,
    ];
}

/** Finds top-level functions/classes anywhere in the AST (incl. nested inside namespace blocks). */
function collectTopLevelDeclarations(array $stmts, array &$functions, array &$classes): void
{
    foreach ($stmts as $stmt) {
        if ($stmt instanceof Stmt\Function_) {
            $functions[] = describeFunction($stmt);
        } elseif ($stmt instanceof Stmt\ClassLike) {
            $classes[] = describeClassLike($stmt);
        } elseif ($stmt instanceof Stmt\Namespace_ || $stmt instanceof Stmt\Block) {
            collectTopLevelDeclarations($stmt->stmts ?? [], $functions, $classes);
        } elseif ($stmt instanceof Stmt\Declare_) {
            collectTopLevelDeclarations($stmt->stmts ?? [], $functions, $classes);
        }
    }
}

function extractFile(string $absolutePath, string $relativePath, $parser): array
{
    $code = file_get_contents($absolutePath);
    if ($code === false) {
        return ['file' => $relativePath, 'error' => 'could not read file', 'functions' => [], 'classes' => []];
    }

    try {
        $ast = $parser->parse($code);
    } catch (Error $e) {
        return ['file' => $relativePath, 'error' => $e->getMessage(), 'functions' => [], 'classes' => []];
    }

    $functions = [];
    $classes = [];
    collectTopLevelDeclarations($ast ?? [], $functions, $classes);

    return ['file' => $relativePath, 'error' => null, 'functions' => $functions, 'classes' => $classes];
}

function main(array $argv): int
{
    if (count($argv) < 2) {
        fwrite(STDERR, "usage: php extract.php <repo_path>\n");
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
