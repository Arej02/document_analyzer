import json

from document_analyzer.worker.extract import ExtractionResult
from document_analyzer.worker.laravel_extractor import LaravelExtractor

_COMPOSER_JSON = json.dumps(
    {
        "require": {"php": "^8.1", "laravel/framework": "^10.0"},
        "autoload": {"psr-4": {"App\\": "app/"}},
    }
)


def _write_app(tmp_path, controller_body: str = "index"):
    (tmp_path / "composer.json").write_text(_COMPOSER_JSON)
    (tmp_path / "routes").mkdir()
    controllers_dir = tmp_path / "app" / "Http" / "Controllers"
    controllers_dir.mkdir(parents=True)
    (controllers_dir / "AppointmentController.php").write_text(
        f"<?php\nnamespace App\\Http\\Controllers;\nclass AppointmentController {{\n"
        f"    public function {controller_body}() {{}}\n"
        f"    public function __invoke() {{}}\n"
        f"    public function store() {{}}\n"
        f"    public function show() {{}}\n"
        f"    public function update() {{}}\n"
        f"    public function destroy() {{}}\n"
        f"    public function create() {{}}\n"
        f"    public function edit() {{}}\n"
        f"}}\n"
    )


def test_detect_false_without_laravel_dependency(tmp_path):
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"php": "^8.1"}}))
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "web.php").write_text("<?php\n")
    assert LaravelExtractor().detect(tmp_path) is False


def test_detect_false_without_route_files(tmp_path):
    (tmp_path / "composer.json").write_text(_COMPOSER_JSON)
    assert LaravelExtractor().detect(tmp_path) is False


def test_detect_true_for_laravel_app_with_routes(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text("<?php\n")
    assert LaravelExtractor().detect(tmp_path) is True


def test_extract_resolves_array_syntax_route(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController;\n"
        "Route::get('/appointments', [AppointmentController::class, 'index']);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    assert isinstance(result, ExtractionResult)
    routes = [e for e in result.entities if e.entity_type == "route"]
    targets = [e for e in result.entities if e.entity_type == "function"]
    [route] = routes
    [target] = targets
    assert route.raw_facts["http_method"] == "GET"
    assert route.raw_facts["path"] == "/appointments"
    assert route.raw_facts["controller"] == "AppointmentController::index"
    assert target.name == "index"
    assert target.file_path == "app/Http/Controllers/AppointmentController.php"
    assert target.raw_facts["class"] == "AppointmentController"
    [edge] = result.edges
    assert edge.edge_type == "route_binding"
    assert edge.from_local_id == target.local_id
    assert edge.to_local_id == route.local_id


def test_extract_resolves_classic_string_syntax_with_default_namespace(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\nRoute::post('/appointments', 'AppointmentController@store');\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    [route] = [e for e in result.entities if e.entity_type == "route"]
    [target] = [e for e in result.entities if e.entity_type == "function"]
    assert route.raw_facts["http_method"] == "POST"
    assert target.name == "store"
    assert target.file_path == "app/Http/Controllers/AppointmentController.php"


def test_extract_resolves_use_alias(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController as ApptCtrl;\n"
        "Route::get('/appointments', [ApptCtrl::class, 'index']);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    [target] = [e for e in result.entities if e.entity_type == "function"]
    assert target.file_path == "app/Http/Controllers/AppointmentController.php"


def test_extract_expands_resource_route(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController;\n"
        "Route::resource('appointments', AppointmentController::class);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    routes = [e for e in result.entities if e.entity_type == "route"]
    actions = sorted(r.raw_facts["action"] for r in routes)
    assert actions == sorted(["index", "create", "store", "show", "edit", "update", "destroy"])
    assert len(result.edges) == 7


def test_extract_expands_api_resource_route_without_create_or_edit(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "api.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController;\n"
        "Route::apiResource('appointments', AppointmentController::class);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    routes = [e for e in result.entities if e.entity_type == "route"]
    actions = sorted(r.raw_facts["action"] for r in routes)
    assert actions == sorted(["index", "store", "show", "update", "destroy"])


def test_extract_resolves_invokable_controller(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController;\n"
        "Route::get('/appointments', AppointmentController::class);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    [target] = [e for e in result.entities if e.entity_type == "function"]
    assert target.name == "__invoke"


def test_extract_skips_closure_targets(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\nRoute::get('/ping', function () {\n    return 'pong';\n});\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    assert result.entities == []
    assert result.edges == []


def test_extract_skips_unresolvable_controller(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\nRoute::get('/ghost', [GhostController::class, 'index']);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    assert result.entities == []
    assert result.edges == []


def test_extract_ignores_chained_middleware_and_name_calls(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController;\n"
        "Route::get('/appointments', [AppointmentController::class, 'index'])"
        "->middleware('auth')->name('appointments.index');\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    routes = [e for e in result.entities if e.entity_type == "route"]
    assert len(routes) == 1
    assert routes[0].raw_facts["path"] == "/appointments"


def test_extract_deduplicates_shared_controller_target(tmp_path):
    _write_app(tmp_path)
    (tmp_path / "routes" / "web.php").write_text(
        "<?php\n"
        "use App\\Http\\Controllers\\AppointmentController;\n"
        "Route::get('/appointments', [AppointmentController::class, 'index']);\n"
        "Route::get('/appointments/list', [AppointmentController::class, 'index']);\n"
    )

    result = LaravelExtractor().extract(tmp_path)

    targets = [e for e in result.entities if e.entity_type == "function"]
    assert len(targets) == 1
    assert len(result.edges) == 2
