from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import click
import frappe


@click.group("native", help="Native app tooling for mobile and desktop clients.")
def native():
	"""Group for native tooling commands."""


@native.group("auth", help="Native authentication scaffolding and setup.")
def native_auth():
	"""Group for native auth commands."""


@native_auth.command("init", help="Configure OAuth auth flow and scaffold login UI for native app.")
@click.option("--app", "app_name", required=True, help="Frappe app name under apps/.")
@click.option("--site", required=True, help="Site where OAuth Client will be created/updated.")
@click.option(
	"--target",
	type=click.Choice(["android"], case_sensitive=False),
	default="android",
	show_default=True,
	help="Native target for auth setup.",
)
@click.option(
	"--base-url",
	default=None,
	help="Public site URL used by the mobile app (e.g. https://erp.example.com).",
)
@click.option("--force", is_flag=True, help="Overwrite custom files that are not recognized as generated.")
def init_native_auth(
	app_name: str,
	site: str,
	target: str,
	base_url: str | None = None,
	force: bool = False,
):
	if target != "android":
		raise click.ClickException("Only Android auth scaffolding is supported right now.")

	bench_path, app_path, android_root = _resolve_android_paths(app_name)
	site_path = bench_path / "sites" / site
	if not site_path.exists():
		raise click.ClickException(f"Site '{site}' was not found at {site_path}.")

	package_id = _resolve_package_name(android_root) or f"com.frappe.{_sanitize_identifier(app_name)}"
	package_path = package_id.replace(".", "/")
	redirect_scheme = _default_redirect_scheme(app_name)
	redirect_host = "oauth-callback"
	redirect_uri = f"{redirect_scheme}://{redirect_host}"

	public_base_url = _resolve_site_base_url(site=site, site_path=site_path, explicit_base_url=base_url)
	oauth_client = _configure_oauth_client_for_site(
		site=site,
		app_name=app_name,
		redirect_uri=redirect_uri,
		scope="all openid",
	)
	client_id = oauth_client["client_id"]

	display_name = _titleize(app_name)
	auth_files = {
		Path("mobile/app/index.html"): _auth_index_template(display_name=display_name),
		Path("mobile/app/styles.css"): _auth_styles_template(),
		Path("mobile/app/app.js"): _auth_app_js_template(),
		Path("mobile/app/auth.config.js"): _auth_config_js_template(
			app_name=app_name,
			site_url=public_base_url,
			bootstrap_method="frappe_native.api.mobile_auth.get_client_id",
			redirect_uri=redirect_uri,
			scope="all openid",
		),
		Path("docs/mobile-auth.md"): _auth_quickstart_template(
			app_name=app_name, site=site, redirect_uri=redirect_uri
		),
		Path("mobile/android/app/src/main/AndroidManifest.xml"): _manifest_template(
			package_id=package_id,
			redirect_scheme=redirect_scheme,
			redirect_host=redirect_host,
		),
		Path(f"mobile/android/app/src/main/java/{package_path}/MainActivity.kt"): _main_activity_template(
			package_id=package_id,
			redirect_scheme=redirect_scheme,
			redirect_host=redirect_host,
		),
	}

	protected_files = {}

	created: list[str] = []
	updated: list[str] = []
	removed: list[str] = []
	for relative_path, content in auth_files.items():
		target_path = app_path / relative_path
		target_path.parent.mkdir(parents=True, exist_ok=True)

		if target_path.exists() and not force:
			guard = protected_files.get(relative_path)
			existing_text = _read_text(target_path)
			if guard and guard not in existing_text:
				raise click.ClickException(
					f"Refusing to overwrite custom file without --force: {target_path}"
				)

		was_existing = target_path.exists()
		target_path.write_text(content, encoding="utf-8")
		(updated if was_existing else created).append(str(relative_path))

	legacy_json = app_path / "mobile" / "app" / "auth.config.json"
	if legacy_json.exists():
		legacy_json.unlink()
		removed.append("mobile/app/auth.config.json")
	legacy_mobile_auth = app_path / app_name / "api" / "mobile_auth.py"
	if legacy_mobile_auth.exists():
		legacy_mobile_auth.unlink()
		removed.append(f"{app_name}/api/mobile_auth.py")
	legacy_api_init = app_path / app_name / "api" / "__init__.py"
	legacy_api_dir = app_path / app_name / "api"
	if legacy_api_init.exists() and _read_text(legacy_api_init).strip() == "":
		remaining = []
		if legacy_api_dir.exists():
			remaining = [
				path
				for path in legacy_api_dir.iterdir()
				if path != legacy_api_init and path.name != "__pycache__"
			]
		if legacy_api_dir.exists() and not remaining:
			legacy_pyc = legacy_api_dir / "__pycache__"
			if legacy_pyc.exists():
				shutil.rmtree(legacy_pyc)
			legacy_api_init.unlink()
			removed.append(f"{app_name}/api/__init__.py")
			legacy_api_dir.rmdir()
			removed.append(f"{app_name}/api/")

	synced_files = _sync_mobile_web_source(app_path=app_path, android_root=android_root)

	click.secho("Native auth scaffold completed.", fg="green")
	click.echo(f"App: {app_name}")
	click.echo(f"Site: {site}")
	click.echo(f"Public URL: {public_base_url}")
	click.echo(f"OAuth Client ID: {client_id}")
	click.echo(f"Redirect URI: {redirect_uri}")
	parsed_base_url = urlparse(public_base_url)
	if parsed_base_url.hostname in {"localhost", "127.0.0.1"}:
		click.secho(
			"Note: localhost/127.0.0.1 is not reachable from a physical phone. Use --base-url with your LAN or public URL.",
			fg=208,
		)

	if created:
		click.echo("\nCreated files:")
		for path in created:
			click.echo(f"  + {path}")
	if updated:
		click.echo("\nUpdated files:")
		for path in updated:
			click.echo(f"  ~ {path}")
	if removed:
		click.echo("\nRemoved files:")
		for path in removed:
			click.echo(f"  - {path}")
	if synced_files:
		click.echo("\nSynced mobile source files:")
		for path in synced_files:
			click.echo(f"  -> {path}")

	click.echo("\nNext steps:")
	click.echo(f"  bench native doctor --app {app_name} --target android")
	click.echo(f"  bench native run --app {app_name} --target android --variant debug")
	click.echo(f"  cat apps/{app_name}/docs/mobile-auth.md")


@native.command("init", help="Scaffold Android MVP native project structure for a Frappe app.")
@click.option("--app", "app_name", required=True, help="Frappe app name under apps/.")
@click.option(
	"--platform",
	type=click.Choice(["android"], case_sensitive=False),
	default="android",
	show_default=True,
	help="Target platform to initialize.",
)
@click.option("--site", default=None, help="Optional site for metadata validation.")
@click.option("--force", is_flag=True, help="Overwrite previously generated files.")
@click.option("--package-id", "package_id", default=None, help="Android package id.")
@click.option("--app-name", "display_name", default=None, help="Display name for generated app.")
def init_native_project(
	app_name: str,
	platform: str,
	site: str | None = None,
	force: bool = False,
	package_id: str | None = None,
	display_name: str | None = None,
):
	if platform != "android":
		raise click.ClickException("Only Android is supported in MVP.")

	bench_path = Path(frappe.utils.get_bench_path())
	app_path = bench_path / "apps" / app_name
	if not app_path.exists():
		raise click.ClickException(f"App '{app_name}' was not found at {app_path}.")

	hooks_path = app_path / app_name / "hooks.py"
	if not hooks_path.exists():
		raise click.ClickException(f"Missing hooks.py for app '{app_name}' at {hooks_path}.")

	if site:
		site_path = bench_path / "sites" / site
		if not site_path.exists():
			raise click.ClickException(f"Site '{site}' was not found at {site_path}.")

	android_root = app_path / "mobile" / "android"
	if android_root.exists() and not force:
		raise click.ClickException(
			f"Target already exists: {android_root}. Use --force to overwrite generated files."
		)

	package_id = package_id or f"com.frappe.{_sanitize_identifier(app_name)}"
	if not _is_valid_package_id(package_id):
		raise click.ClickException(
			"Invalid --package-id. Use Java package format like 'com.example.app'."
		)

	display_name = display_name or _titleize(app_name)
	package_path = package_id.replace(".", "/")
	redirect_scheme = _default_redirect_scheme(app_name)
	redirect_host = "oauth-callback"

	files_to_write = _get_android_mvp_templates(
		app_name=app_name,
		display_name=display_name,
		package_id=package_id,
		package_path=package_path,
		redirect_scheme=redirect_scheme,
		redirect_host=redirect_host,
	)

	created = []
	updated = []
	for relative_path, content in files_to_write.items():
		target = app_path / relative_path
		target.parent.mkdir(parents=True, exist_ok=True)
		if target.exists():
			if not force:
				raise click.ClickException(
					f"Refusing to overwrite existing file without --force: {target}"
				)
			target.write_text(content, encoding="utf-8")
			updated.append(str(relative_path))
		else:
			target.write_text(content, encoding="utf-8")
			created.append(str(relative_path))

	synced_files = _sync_mobile_web_source(app_path=app_path, android_root=android_root)
	setup_notes = _post_init_android_setup(android_root=android_root, force=force)

	click.secho("Native Android MVP scaffold completed.", fg="green")
	click.echo(f"App: {app_name}")
	click.echo(f"Package ID: {package_id}")
	click.echo(f"Display Name: {display_name}")

	if created:
		click.echo("\nCreated files:")
		for path in created:
			click.echo(f"  + {path}")

	if updated:
		click.echo("\nUpdated files:")
		for path in updated:
			click.echo(f"  ~ {path}")

	if setup_notes:
		click.echo("\nSetup notes:")
		for note in setup_notes:
			click.echo(f"  - {note}")
	if synced_files:
		click.echo("\nSynced mobile source files:")
		for path in synced_files:
			click.echo(f"  -> {path}")

	click.echo("\nNext steps:")
	click.echo(f"  bench native doctor --app {app_name} --target android")
	click.echo(f"  bench native auth init --app {app_name} --site <your-site>")
	click.echo(f"  bench native build --app {app_name} --target android --variant debug")
	click.echo(f"  bench native run --app {app_name} --target android --variant debug")
	click.echo(f"  cat apps/{app_name}/docs/mobile-quickstart.md")


@native.command("doctor", help="Validate Android native project readiness for an app.")
@click.option("--app", "app_name", required=True, help="Frappe app name under apps/.")
@click.option(
	"--target",
	type=click.Choice(["android"], case_sensitive=False),
	default="android",
	show_default=True,
	help="Native target to validate.",
)
@click.option("--strict", is_flag=True, help="Treat warnings as failures.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON output.")
@click.option("--build-check", is_flag=True, help="Run `./gradlew assembleDebug` smoke test.")
def doctor_native_project(
	app_name: str,
	target: str,
	strict: bool = False,
	json_output: bool = False,
	build_check: bool = False,
):
	if target != "android":
		raise click.ClickException("Only Android doctor checks are supported right now.")

	bench_path = Path(frappe.utils.get_bench_path())
	app_path = bench_path / "apps" / app_name
	android_root = app_path / "mobile" / "android"
	checks: list[dict] = []

	_add_check(
		checks,
		key="app.path",
		status="pass" if app_path.exists() else "fail",
		message=f"App path {'found' if app_path.exists() else 'missing'}: {app_path}",
		fix=None if app_path.exists() else f"Create app first: bench new-app {app_name}",
	)

	hooks_path = app_path / app_name / "hooks.py"
	_add_check(
		checks,
		key="app.hooks",
		status="pass" if hooks_path.exists() else "fail",
		message=f"hooks.py {'found' if hooks_path.exists() else 'missing'}: {hooks_path}",
		fix=None if hooks_path.exists() else f"Expected Frappe app package at {app_name}/{app_name}/hooks.py",
	)

	_add_check(
		checks,
		key="android.scaffold",
		status="pass" if android_root.exists() else "fail",
		message=f"Android scaffold {'found' if android_root.exists() else 'missing'}: {android_root}",
		fix=None
		if android_root.exists()
		else f"Run: bench native init --app {app_name} --platform android",
	)

	settings_gradle = android_root / "settings.gradle.kts"
	settings_ok = settings_gradle.exists()
	_add_check(
		checks,
		key="android.settings",
		status="pass" if settings_ok else "fail",
		message=f"settings.gradle.kts {'found' if settings_ok else 'missing'}",
		fix=None if settings_ok else f"Run: bench native init --app {app_name} --platform android --force",
	)

	if settings_ok:
		settings_text = _read_text(settings_gradle)
		has_plugin_repos = all(
			token in settings_text for token in ("pluginManagement", "google()", "mavenCentral()")
		)
		_add_check(
			checks,
			key="android.repositories",
			status="pass" if has_plugin_repos else "fail",
			message="Plugin repositories configured in settings.gradle.kts",
			fix=None
			if has_plugin_repos
			else f"Regenerate scaffold: bench native init --app {app_name} --platform android --force",
		)

	main_activity = _resolve_main_activity(android_root)
	_add_check(
		checks,
		key="android.main_activity",
		status="pass" if main_activity else "fail",
		message=f"MainActivity {'found' if main_activity else 'missing'}",
		fix=None
		if main_activity
		else f"Regenerate scaffold: bench native init --app {app_name} --platform android --force",
	)

	if main_activity:
		main_activity_text = _read_text(main_activity)
		loads_asset = 'file:///android_asset/frappe_native/index.html' in main_activity_text
		_add_check(
			checks,
			key="android.start_page_mode",
			status="pass" if loads_asset else "warn",
			message=(
				"MainActivity loads bundled local asset page"
				if loads_asset
				else "MainActivity does not load bundled local asset page by default"
			),
			fix=(
				"Set start page to file:///android_asset/frappe_native/index.html or re-run init --force"
				if not loads_asset
				else None
			),
		)

	mobile_source_root = _mobile_source_root(app_path)
	source_index = mobile_source_root / "index.html"
	_add_check(
		checks,
		key="mobile.source_dir",
		status="pass" if mobile_source_root.exists() else "fail",
		message=f"Mobile source dir {'found' if mobile_source_root.exists() else 'missing'}: {mobile_source_root}",
		fix=(
			None
			if mobile_source_root.exists()
			else f"Run: bench native init --app {app_name} --platform android --force"
		),
	)
	_add_check(
		checks,
		key="mobile.source_index",
		status="pass" if source_index.exists() else "fail",
		message=f"Editable index.html {'found' if source_index.exists() else 'missing'}: {source_index}",
		fix=(
			None
			if source_index.exists()
			else f"Run: bench native init --app {app_name} --platform android --force"
		),
	)

	asset_index = _android_asset_root(android_root) / "index.html"
	_add_check(
		checks,
		key="android.start_page_file",
		status="pass" if asset_index.exists() else "warn",
		message=f"Android asset index.html {'found' if asset_index.exists() else 'missing'}: {asset_index}",
		fix=None
		if asset_index.exists()
		else f"Run: bench native build --app {app_name} --target android --variant debug (syncs source to assets)",
	)

	gradlew_path = android_root / "gradlew"
	_add_check(
		checks,
		key="android.gradle_wrapper",
		status="pass" if gradlew_path.exists() else "warn",
		message=f"Gradle wrapper {'found' if gradlew_path.exists() else 'missing'}: {gradlew_path}",
		fix=None
		if gradlew_path.exists()
		else f"cd {android_root} && gradle wrapper --gradle-version 8.7",
	)

	java_version = _get_java_major_version()
	if java_version is None:
		_add_check(
			checks,
			key="tool.java",
			status="fail",
			message="Java is not available in PATH",
			fix="Install JDK 17 and export JAVA_HOME",
		)
	elif java_version == 17:
		_add_check(
			checks,
			key="tool.java",
			status="pass",
			message="Java 17 detected",
		)
	else:
		_add_check(
			checks,
			key="tool.java",
			status="warn",
			message=f"Java {java_version} detected (recommended: 17 for consistent Android builds)",
			fix="Export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64",
		)

	gradle_version = _get_gradle_version()
	if gradle_version:
		_add_check(checks, key="tool.gradle", status="pass", message=f"Gradle {gradle_version} detected")
	else:
		_add_check(
			checks,
			key="tool.gradle",
			status="warn",
			message="Gradle not found in PATH",
			fix="Install Gradle 8.x (or rely on existing ./gradlew)",
		)

	local_properties = android_root / "local.properties"
	sdk_dir = _get_android_sdk_dir(local_properties)
	if sdk_dir is None:
		_add_check(
			checks,
			key="android.sdk_dir",
			status="fail",
			message="Android SDK path is not configured",
			fix=(
				f"Set sdk.dir in {local_properties} or export ANDROID_SDK_ROOT=/path/to/Android/Sdk"
			),
		)
	else:
		sdk_path = Path(sdk_dir).expanduser()
		_add_check(
			checks,
			key="android.sdk_dir",
			status="pass" if sdk_path.exists() else "fail",
			message=f"Android SDK path: {sdk_path}",
			fix=None
			if sdk_path.exists()
			else f"Install Android SDK and update {local_properties} (sdk.dir=...)",
		)
		if sdk_path.exists():
			_check_required_android_sdk(checks, sdk_path)
			_check_adb(checks, sdk_path)
		else:
			_add_check(
				checks,
				key="android.adb",
				status="warn",
				message="Skipping adb checks because SDK path does not exist",
			)

	apk_path = android_root / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
	_add_check(
		checks,
		key="android.debug_apk",
		status="pass" if apk_path.exists() else "warn",
		message=f"Debug APK {'found' if apk_path.exists() else 'not found'}: {apk_path}",
		fix=None if apk_path.exists() else f"cd {android_root} && ./gradlew assembleDebug",
	)

	if build_check:
		if gradlew_path.exists():
			ok, output = _run_gradle_assemble(android_root)
			_add_check(
				checks,
				key="android.build_smoke",
				status="pass" if ok else "fail",
				message="Gradle assembleDebug succeeded" if ok else "Gradle assembleDebug failed",
				fix=None if ok else output,
			)
		else:
			_add_check(
				checks,
				key="android.build_smoke",
				status="fail",
				message="Cannot run build smoke test without ./gradlew",
				fix=f"cd {android_root} && gradle wrapper --gradle-version 8.7",
			)

	summary = _build_summary(checks)
	should_fail = summary["fail"] > 0 or (strict and summary["warn"] > 0)

	if json_output:
		click.echo(
			json.dumps(
				{
					"app": app_name,
					"target": target,
					"checks": checks,
					"summary": summary,
					"strict": strict,
					"ok": not should_fail,
				},
				indent=2,
			)
		)
	else:
		click.echo(f"Native doctor report for app '{app_name}' ({target})")
		for check in checks:
			_status_line(check)
		click.echo(
			f"\nSummary: {summary['pass']} pass, {summary['warn']} warn, {summary['fail']} fail"
		)
		if should_fail:
			click.secho("Doctor status: NOT READY", fg="red")
		else:
			click.secho("Doctor status: READY", fg="green")

	if should_fail:
		sys.exit(1)


@native.command("build", help="Build Android APK for an app.")
@click.option("--app", "app_name", required=True, help="Frappe app name under apps/.")
@click.option(
	"--target",
	type=click.Choice(["android"], case_sensitive=False),
	default="android",
	show_default=True,
	help="Native target to build.",
)
@click.option(
	"--variant",
	type=click.Choice(["debug", "release"], case_sensitive=False),
	default="debug",
	show_default=True,
	help="Android build variant.",
)
@click.option("--install", is_flag=True, help="Install APK via adb after a successful debug build.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON output.")
def build_native_project(
	app_name: str,
	target: str,
	variant: str,
	install: bool = False,
	json_output: bool = False,
):
	if target != "android":
		raise click.ClickException("Only Android builds are supported right now.")

	_, app_path, android_root = _resolve_android_paths(app_name)
	gradle_task, apk_paths = _build_android_variant(
		app_path=app_path, android_root=android_root, app_name=app_name, variant=variant, verbose=not json_output
	)

	installed_apk_path = None
	if install:
		if variant != "debug":
			raise click.ClickException("--install is currently supported only with --variant debug.")
		if not apk_paths:
			raise click.ClickException("Build succeeded but no APK was found to install.")

		adb_path = _resolve_adb_path(android_root)
		if adb_path is None:
			raise click.ClickException("adb not found. Install Android SDK Platform-Tools and ensure adb is in PATH.")

		install_ok, install_message = _install_apk(adb_path=adb_path, apk_path=apk_paths[0])
		if not install_ok:
			raise click.ClickException(install_message)
		installed_apk_path = apk_paths[0]

	if json_output:
		click.echo(
			json.dumps(
				{
					"app": app_name,
					"target": target,
					"variant": variant,
					"ok": True,
					"task": gradle_task,
					"apk_paths": [str(path) for path in apk_paths],
					"installed": bool(installed_apk_path),
					"installed_apk_path": str(installed_apk_path) if installed_apk_path else None,
				},
				indent=2,
			)
		)
		return

	click.secho("Build completed successfully.", fg="green")
	if apk_paths:
		click.echo("APK output:")
		for apk_path in apk_paths:
			click.echo(f"  {apk_path}")
	else:
		click.echo(
			f"Build succeeded but no APK found under {android_root / 'app' / 'build' / 'outputs' / 'apk' / variant}"
		)
	if installed_apk_path:
		click.secho(f"Installed on device: {installed_apk_path}", fg="green")


@native.command("run", help="Build, install, and launch Android app on connected device.")
@click.option("--app", "app_name", required=True, help="Frappe app name under apps/.")
@click.option(
	"--target",
	type=click.Choice(["android"], case_sensitive=False),
	default="android",
	show_default=True,
	help="Native target to run.",
)
@click.option(
	"--variant",
	type=click.Choice(["debug", "release"], case_sensitive=False),
	default="debug",
	show_default=True,
	help="Android build variant.",
)
@click.option("--live", is_flag=True, help="Watch mobile source and rebuild/install/relaunch on changes.")
@click.option("--logs", is_flag=True, help="Stream WebView console logs from adb logcat.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON output.")
def run_native_project(
	app_name: str,
	target: str,
	variant: str,
	live: bool = False,
	logs: bool = False,
	json_output: bool = False,
):
	if target != "android":
		raise click.ClickException("Only Android run is supported right now.")
	if variant != "debug":
		raise click.ClickException("`bench native run` currently supports only --variant debug.")
	if json_output and (live or logs):
		raise click.ClickException("--json cannot be combined with --live or --logs.")

	_, app_path, android_root = _resolve_android_paths(app_name)
	adb_path = _resolve_adb_path(android_root)
	if adb_path is None:
		raise click.ClickException("adb not found. Install Android SDK Platform-Tools and ensure adb is in PATH.")

	package_name = _resolve_package_name(android_root)
	if not package_name:
		raise click.ClickException("Could not determine applicationId for launching app.")

	gradle_task, apk_path, install_message, launch_message = _run_once_install_and_launch(
		app_name=app_name,
		app_path=app_path,
		android_root=android_root,
		variant=variant,
		adb_path=adb_path,
		package_name=package_name,
		verbose=not json_output,
	)

	if json_output:
		click.echo(
			json.dumps(
				{
					"app": app_name,
					"target": target,
					"variant": variant,
					"ok": True,
					"task": gradle_task,
					"apk_path": str(apk_path),
					"package": package_name,
					"install": install_message,
					"launch": launch_message,
				},
				indent=2,
			)
		)
		return

	click.secho("Run completed successfully.", fg="green")
	click.echo(f"APK installed: {apk_path}")
	click.echo(f"Launched package: {package_name}")

	logcat_process = None
	logcat_thread = None
	stop_logcat = threading.Event()
	try:
		if logs:
			logcat_process, logcat_thread = _start_logcat_stream(
				adb_path=adb_path, stop_event=stop_logcat, tag="FrappeNativeWebView"
			)
			click.echo("Streaming WebView console logs (Ctrl+C to stop)...")

		if live:
			click.echo(f"Live reload watching: {_mobile_source_root(app_path)}")
			click.echo("On file change: sync -> build -> install -> relaunch")
			click.echo("Press Ctrl+C to stop.")
			previous = _snapshot_mobile_source(_mobile_source_root(app_path))
			try:
				while True:
					time.sleep(1.0)
					current = _snapshot_mobile_source(_mobile_source_root(app_path))
					if current == previous:
						continue

					changed_files = _diff_mobile_snapshots(previous, current)
					previous = current
					changed_line = ", ".join(changed_files[:5])
					if len(changed_files) > 5:
						changed_line += f", +{len(changed_files) - 5} more"
					click.echo(f"\nDetected changes: {changed_line}")

					try:
						_, rebuilt_apk, _, _ = _run_once_install_and_launch(
							app_name=app_name,
							app_path=app_path,
							android_root=android_root,
							variant=variant,
							adb_path=adb_path,
							package_name=package_name,
							verbose=False,
						)
						click.secho(f"Reloaded successfully: {rebuilt_apk}", fg="green")
					except click.ClickException as error:
						click.secho(f"Reload failed: {error}", fg="red")
			except KeyboardInterrupt:
				click.echo("\nStopped live reload.")
		elif logs:
			try:
				while True:
					time.sleep(0.5)
			except KeyboardInterrupt:
				click.echo("\nStopped log streaming.")
	finally:
		if stop_logcat:
			stop_logcat.set()
		if logcat_process:
			_stop_process(logcat_process)
		if logcat_thread:
			logcat_thread.join(timeout=2)


def _add_check(
	checks: list[dict],
	key: str,
	status: str,
	message: str,
	fix: str | None = None,
) -> None:
	check = {"key": key, "status": status, "message": message}
	if fix:
		check["fix"] = fix
	checks.append(check)


def _read_text(path: Path) -> str:
	try:
		return path.read_text(encoding="utf-8")
	except Exception:
		return ""


def _resolve_main_activity(android_root: Path) -> Path | None:
	base = android_root / "app" / "src" / "main" / "java"
	if not base.exists():
		return None
	main_files = sorted(base.glob("**/MainActivity.kt"))
	return main_files[0] if main_files else None


def _extract_major_version(raw: str) -> int | None:
	match = re.search(r'version "(\d+)', raw)
	if match:
		return int(match.group(1))
	return None


def _get_java_major_version() -> int | None:
	java_bin = shutil.which("java")
	if not java_bin:
		return None
	try:
		proc = subprocess.run(
			[java_bin, "-version"],
			check=False,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			timeout=10,
		)
	except (subprocess.SubprocessError, OSError):
		return None

	raw = (proc.stderr or "") + "\n" + (proc.stdout or "")
	return _extract_major_version(raw)


def _get_gradle_version() -> str | None:
	gradle_bin = shutil.which("gradle")
	if not gradle_bin:
		return None
	try:
		proc = subprocess.run(
			[gradle_bin, "-v"],
			check=False,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			timeout=10,
		)
	except (subprocess.SubprocessError, OSError):
		return None

	for line in (proc.stdout or "").splitlines():
		if line.strip().startswith("Gradle "):
			return line.strip().replace("Gradle ", "")
	return None


def _parse_local_properties(path: Path) -> dict[str, str]:
	if not path.exists():
		return {}
	props = {}
	for raw_line in _read_text(path).splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		props[key.strip()] = value.strip().replace("\\\\", "\\")
	return props


def _get_android_sdk_dir(local_properties: Path) -> str | None:
	props = _parse_local_properties(local_properties)
	if props.get("sdk.dir"):
		return props["sdk.dir"]
	return os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")


def _resolve_adb_path(android_root: Path) -> Path | None:
	adb_from_path = shutil.which("adb")
	if adb_from_path:
		return Path(adb_from_path)

	local_properties = android_root / "local.properties"
	sdk_dir = _get_android_sdk_dir(local_properties)
	if not sdk_dir:
		return None

	sdk_path = Path(sdk_dir).expanduser()
	candidate = sdk_path / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
	return candidate if candidate.exists() else None


def _check_required_android_sdk(checks: list[dict], sdk_path: Path) -> None:
	platform_tools = sdk_path / "platform-tools"
	platform_34 = sdk_path / "platforms" / "android-34"
	build_tools_dir = sdk_path / "build-tools"
	has_build_tools_34 = any(
		item.is_dir() and item.name.startswith("34.") for item in build_tools_dir.glob("*")
	) if build_tools_dir.exists() else False

	_add_check(
		checks,
		key="android.sdk.platform_tools",
		status="pass" if platform_tools.exists() else "fail",
		message=f"SDK platform-tools {'found' if platform_tools.exists() else 'missing'}",
		fix=None if platform_tools.exists() else "Install Android SDK Platform-Tools in Android Studio SDK Manager",
	)
	_add_check(
		checks,
		key="android.sdk.platform_34",
		status="pass" if platform_34.exists() else "fail",
		message=f"SDK platform android-34 {'found' if platform_34.exists() else 'missing'}",
		fix=None if platform_34.exists() else "Install Android SDK Platform 34 in Android Studio SDK Manager",
	)
	_add_check(
		checks,
		key="android.sdk.build_tools_34",
		status="pass" if has_build_tools_34 else "fail",
		message="SDK build-tools 34.x " + ("found" if has_build_tools_34 else "missing"),
		fix=None if has_build_tools_34 else "Install Android SDK Build-Tools 34.x in Android Studio SDK Manager",
	)


def _check_adb(checks: list[dict], sdk_path: Path) -> None:
	adb_from_path = shutil.which("adb")
	adb_from_sdk = sdk_path / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
	adb_path = Path(adb_from_path) if adb_from_path else (adb_from_sdk if adb_from_sdk.exists() else None)

	_add_check(
		checks,
		key="android.adb.binary",
		status="pass" if adb_path else "fail",
		message=f"adb {'found' if adb_path else 'not found'}"
		+ (f": {adb_path}" if adb_path else ""),
		fix=None if adb_path else "Ensure Android SDK Platform-Tools are installed and adb is in PATH",
	)

	if not adb_path:
		return

	try:
		proc = subprocess.run(
			[str(adb_path), "devices"],
			check=False,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			timeout=10,
		)
	except (subprocess.SubprocessError, OSError):
		_add_check(
			checks,
			key="android.adb.devices",
			status="warn",
			message="Could not query adb devices",
			fix="Run `adb start-server` and reconnect your device",
		)
		return

	device_lines = [
		line for line in (proc.stdout or "").splitlines()[1:] if line.strip().endswith("\tdevice")
	]
	_add_check(
		checks,
		key="android.adb.devices",
		status="pass" if device_lines else "warn",
		message=f"Connected devices: {len(device_lines)}",
		fix=None
		if device_lines
		else "Connect device with USB debugging enabled, then run `adb devices`",
	)


def _mobile_source_root(app_path: Path) -> Path:
	return app_path / "mobile" / "app"


def _android_asset_root(android_root: Path) -> Path:
	return android_root / "app" / "src" / "main" / "assets" / "frappe_native"


def _sync_mobile_web_source(app_path: Path, android_root: Path) -> list[str]:
	source_root = _mobile_source_root(app_path)
	if not source_root.exists():
		return []

	asset_root = _android_asset_root(android_root)
	asset_root.mkdir(parents=True, exist_ok=True)
	synced: list[str] = []

	for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
		relative = source.relative_to(source_root)
		target = asset_root / relative
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(source, target)
		synced.append(str(relative))

	return synced


def _resolve_android_paths(app_name: str) -> tuple[Path, Path, Path]:
	bench_path = Path(frappe.utils.get_bench_path())
	app_path = bench_path / "apps" / app_name
	android_root = app_path / "mobile" / "android"

	if not app_path.exists():
		raise click.ClickException(f"App '{app_name}' was not found at {app_path}.")
	if not android_root.exists():
		raise click.ClickException(
			f"Android scaffold not found at {android_root}. Run `bench native init --app {app_name} --platform android` first."
		)
	return bench_path, app_path, android_root


def _ensure_gradle_wrapper(android_root: Path) -> list[str]:
	gradlew_cmd = _get_gradle_wrapper_cmd(android_root)
	if gradlew_cmd:
		return gradlew_cmd

	gradle_bin = shutil.which("gradle")
	if not gradle_bin:
		raise click.ClickException(
			"Gradle wrapper is missing and `gradle` is not in PATH. "
			f"Run `cd {android_root} && gradle wrapper --gradle-version 8.7` first."
		)
	try:
		subprocess.run(
			[gradle_bin, "wrapper", "--gradle-version", "8.7"],
			cwd=android_root,
			check=True,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
		)
	except subprocess.CalledProcessError as error:
		last_line = _last_non_empty_line(error.stderr) or _last_non_empty_line(error.stdout)
		raise click.ClickException(
			f"Failed to generate Gradle wrapper automatically: {last_line or 'unknown error'}"
		) from error

	gradlew_cmd = _get_gradle_wrapper_cmd(android_root)
	if gradlew_cmd is None:
		raise click.ClickException("Gradle wrapper generation completed but wrapper executable was not found.")
	return gradlew_cmd


def _build_android_variant(
	app_path: Path, android_root: Path, app_name: str, variant: str, verbose: bool = True
) -> tuple[str, list[Path]]:
	_sync_mobile_web_source(app_path=app_path, android_root=android_root)
	gradlew_cmd = _ensure_gradle_wrapper(android_root)
	gradle_task = f"assemble{variant.capitalize()}"
	if verbose:
		click.echo(f"Running {gradle_task} for '{app_name}' (after syncing mobile/app -> android assets) ...")

	build_ok, build_message = _run_gradle_task(android_root, gradlew_cmd, gradle_task)
	if not build_ok:
		raise click.ClickException(build_message)

	return gradle_task, _find_apk_outputs(android_root, variant)


def _install_apk(adb_path: Path, apk_path: Path) -> tuple[bool, str]:
	try:
		proc = subprocess.run(
			[str(adb_path), "install", "-r", str(apk_path)],
			check=False,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			timeout=120,
		)
	except subprocess.TimeoutExpired:
		return False, "adb install timed out"
	except (subprocess.SubprocessError, OSError) as error:
		return False, f"adb install failed: {error}"

	output = "\n".join(filter(None, [proc.stdout.strip(), proc.stderr.strip()])).strip()
	if proc.returncode != 0:
		return False, output or "adb install failed"
	if "Success" not in output:
		return False, output or "adb install did not report Success"
	return True, "adb install succeeded"


def _resolve_package_name(android_root: Path) -> str | None:
	build_gradle = android_root / "app" / "build.gradle.kts"
	text = _read_text(build_gradle)
	match = re.search(r'applicationId\s*=\s*"([^"]+)"', text)
	if match:
		return match.group(1)
	main_activity = _resolve_main_activity(android_root)
	if main_activity:
		main_text = _read_text(main_activity)
		package_match = re.search(r"^package\s+([a-zA-Z0-9_\\.]+)", main_text, flags=re.MULTILINE)
		if package_match:
			return package_match.group(1)
	return None


def _launch_app(adb_path: Path, package_name: str) -> tuple[bool, str]:
	try:
		proc = subprocess.run(
			[
				str(adb_path),
				"shell",
				"monkey",
				"-p",
				package_name,
				"-c",
				"android.intent.category.LAUNCHER",
				"1",
			],
			check=False,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			timeout=30,
		)
	except subprocess.TimeoutExpired:
		return False, "App launch timed out"
	except (subprocess.SubprocessError, OSError) as error:
		return False, f"Failed to launch app: {error}"

	output = "\n".join(filter(None, [proc.stdout.strip(), proc.stderr.strip()])).strip()
	if proc.returncode != 0:
		return False, output or f"Failed to launch package {package_name}"
	return True, f"Launched package {package_name}"


def _run_once_install_and_launch(
	app_name: str,
	app_path: Path,
	android_root: Path,
	variant: str,
	adb_path: Path,
	package_name: str,
	verbose: bool = True,
) -> tuple[str, Path, str, str]:
	gradle_task, apk_paths = _build_android_variant(
		app_path=app_path,
		android_root=android_root,
		app_name=app_name,
		variant=variant,
		verbose=verbose,
	)
	if not apk_paths:
		raise click.ClickException("Build succeeded but no APK was found to install/run.")

	install_ok, install_message = _install_apk(adb_path=adb_path, apk_path=apk_paths[0])
	if not install_ok:
		raise click.ClickException(install_message)

	launch_ok, launch_message = _launch_app(adb_path=adb_path, package_name=package_name)
	if not launch_ok:
		raise click.ClickException(launch_message)

	return gradle_task, apk_paths[0], install_message, launch_message


def _snapshot_mobile_source(source_root: Path) -> dict[str, tuple[int, int]]:
	if not source_root.exists():
		return {}
	snapshot: dict[str, tuple[int, int]] = {}
	for path in sorted(source_root.rglob("*")):
		if not path.is_file():
			continue
		relative = str(path.relative_to(source_root))
		stat = path.stat()
		snapshot[relative] = (stat.st_mtime_ns, stat.st_size)
	return snapshot


def _diff_mobile_snapshots(
	before: dict[str, tuple[int, int]],
	after: dict[str, tuple[int, int]],
) -> list[str]:
	keys = sorted(set(before) | set(after))
	changed = [key for key in keys if before.get(key) != after.get(key)]
	return changed or ["(unknown changes)"]


def _start_logcat_stream(
	adb_path: Path,
	stop_event: threading.Event,
	tag: str,
) -> tuple[subprocess.Popen | None, threading.Thread | None]:
	try:
		proc = subprocess.Popen(
			[str(adb_path), "logcat", f"{tag}:V", "*:S"],
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			bufsize=1,
		)
	except (subprocess.SubprocessError, OSError):
		click.secho("Could not start logcat streaming.", fg=208)
		return None, None

	def _reader():
		if proc.stdout is None:
			return
		for line in proc.stdout:
			if stop_event.is_set():
				break
			text = line.rstrip()
			if text:
				click.echo(f"[web] {text}")

	thread = threading.Thread(target=_reader, daemon=True)
	thread.start()
	return proc, thread


def _stop_process(proc: subprocess.Popen) -> None:
	if proc.poll() is not None:
		return
	proc.terminate()
	try:
		proc.wait(timeout=2)
	except subprocess.TimeoutExpired:
		proc.kill()


def _get_gradle_wrapper_cmd(android_root: Path) -> list[str] | None:
	if os.name == "nt":
		gradlew = android_root / "gradlew.bat"
		return [str(gradlew)] if gradlew.exists() else None
	gradlew = android_root / "gradlew"
	return ["./gradlew"] if gradlew.exists() else None


def _run_gradle_task(android_root: Path, gradlew_cmd: list[str], task: str) -> tuple[bool, str]:
	try:
		proc = subprocess.run(
			[*gradlew_cmd, task],
			cwd=android_root,
			check=False,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			timeout=600,
		)
	except subprocess.TimeoutExpired:
		return False, "Build timed out after 10 minutes"
	except (subprocess.SubprocessError, OSError) as error:
		return False, str(error)

	if proc.returncode == 0:
		return True, "Build succeeded"

	error_line = _last_non_empty_line(proc.stderr) or _last_non_empty_line(proc.stdout)
	return False, error_line or "Build failed"


def _find_apk_outputs(android_root: Path, variant: str) -> list[Path]:
	apk_dir = android_root / "app" / "build" / "outputs" / "apk" / variant
	if not apk_dir.exists():
		return []
	return sorted(path for path in apk_dir.glob("*.apk") if path.is_file())


def _run_gradle_assemble(android_root: Path) -> tuple[bool, str]:
	gradlew_cmd = _get_gradle_wrapper_cmd(android_root)
	if gradlew_cmd is None:
		return False, "Gradle wrapper not found"
	return _run_gradle_task(android_root, gradlew_cmd, "assembleDebug")


def _build_summary(checks: list[dict]) -> dict[str, int]:
	return {
		"pass": sum(1 for check in checks if check["status"] == "pass"),
		"warn": sum(1 for check in checks if check["status"] == "warn"),
		"fail": sum(1 for check in checks if check["status"] == "fail"),
	}


def _status_line(check: dict) -> None:
	label = check["status"].upper().ljust(4)
	color = {"pass": "green", "warn": 208, "fail": "red"}[check["status"]]
	click.secho(label, fg=color, nl=False)
	click.echo(f" {check['key']}: {check['message']}")
	if check.get("fix"):
		click.echo(f"     fix: {check['fix']}")


def _post_init_android_setup(android_root: Path, force: bool) -> list[str]:
	notes = []
	sdk_dir = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
	local_properties = android_root / "local.properties"

	if sdk_dir and (force or not local_properties.exists()):
		local_properties.write_text(f"sdk.dir={_escape_local_properties_path(sdk_dir)}\n", encoding="utf-8")
		notes.append(f"Wrote local.properties from SDK env: {sdk_dir}")
	elif sdk_dir:
		notes.append("Kept existing local.properties")
	else:
		notes.append("ANDROID_SDK_ROOT/ANDROID_HOME not set; set sdk.dir manually in local.properties")

	gradle_bin = shutil.which("gradle")
	gradlew = android_root / "gradlew"
	if gradle_bin and not gradlew.exists():
		try:
			subprocess.run(
				[gradle_bin, "wrapper", "--gradle-version", "8.7"],
				cwd=android_root,
				check=True,
				timeout=60,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
				text=True,
			)
			notes.append("Generated Gradle wrapper: ./gradlew")
		except subprocess.CalledProcessError as error:
			last_line = _last_non_empty_line(error.stderr) or _last_non_empty_line(error.stdout)
			notes.append(
				f"Could not generate Gradle wrapper automatically ({last_line or 'unknown error'})."
			)
		except subprocess.TimeoutExpired:
			notes.append("Timed out while generating wrapper; run `gradle wrapper --gradle-version 8.7` manually")
	elif gradle_bin:
		notes.append("Gradle wrapper already present: ./gradlew")
	else:
		notes.append("Gradle not found in PATH; install Gradle 8.x to generate wrapper")

	return notes


def _last_non_empty_line(value: str | None) -> str:
	if not value:
		return ""
	lines = [line.strip() for line in value.splitlines() if line.strip()]
	return lines[-1] if lines else ""


def _escape_local_properties_path(path: str) -> str:
	return path.replace("\\", "\\\\")


def _sanitize_identifier(value: str) -> str:
	cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", value).lower()
	cleaned = re.sub(r"_+", "_", cleaned).strip("_")
	return cleaned or "app"


def _titleize(value: str) -> str:
	return " ".join(part.capitalize() for part in re.split(r"[_\-\s]+", value) if part)


def _is_valid_package_id(value: str) -> bool:
	pattern = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
	return bool(pattern.match(value))


def _default_redirect_scheme(app_name: str) -> str:
	slug = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-")
	slug = slug or "app"
	return f"frappe-{slug}"


def _normalize_base_url(value: str) -> str:
	candidate = value.strip()
	if not candidate:
		raise click.ClickException("Base URL cannot be empty.")
	if "://" not in candidate:
		candidate = f"https://{candidate}"

	parsed = urlparse(candidate)
	if parsed.scheme not in {"http", "https"} or not parsed.netloc:
		raise click.ClickException(f"Invalid base URL: {value}")

	return candidate.rstrip("/")


def _resolve_site_base_url(site: str, site_path: Path, explicit_base_url: str | None = None) -> str:
	if explicit_base_url:
		return _normalize_base_url(explicit_base_url)

	site_config_path = site_path / "site_config.json"
	if site_config_path.exists():
		try:
			site_config = json.loads(site_config_path.read_text(encoding="utf-8"))
			host_name = site_config.get("host_name")
			if host_name:
				return _normalize_base_url(str(host_name))
		except Exception:
			pass

	def _get_url() -> str:
		return frappe.utils.get_url()

	try:
		return _normalize_base_url(_run_in_site_context(site, _get_url))
	except Exception:
		return _normalize_base_url(f"https://{site}")


def _run_in_site_context(site: str, fn):
	frappe.init(site=site)
	frappe.connect()
	try:
		return fn()
	finally:
		if getattr(frappe.local, "db", None):
			frappe.db.close()
		frappe.destroy()


def _configure_oauth_client_for_site(
	site: str,
	app_name: str,
	redirect_uri: str,
	scope: str = "all openid",
) -> dict[str, str]:
	client_app_name = f"Frappe Native - {_titleize(app_name)}"

	def _configure() -> dict[str, str]:
		installed_apps = set(frappe.get_installed_apps())
		if "frappe_native" not in installed_apps:
			raise click.ClickException(
				f"`frappe_native` is not installed on site '{site}'. "
				f"Run: bench --site {site} install-app frappe_native"
			)

		existing_name = frappe.db.get_value("OAuth Client", {"app_name": client_app_name}, "name")
		oauth_client = frappe.get_doc("OAuth Client", existing_name) if existing_name else frappe.new_doc("OAuth Client")

		oauth_client.app_name = client_app_name
		oauth_client.scopes = scope
		oauth_client.redirect_uris = redirect_uri
		oauth_client.default_redirect_uri = redirect_uri
		oauth_client.grant_type = "Authorization Code"
		oauth_client.response_type = "Code"
		oauth_client.skip_authorization = 1
		oauth_client.set("allowed_roles", [])
		oauth_client.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"name": oauth_client.name,
			"client_id": oauth_client.client_id or oauth_client.name,
			"redirect_uri": redirect_uri,
			"scope": scope,
		}

	return _run_in_site_context(site, _configure)


def _get_android_mvp_templates(
	app_name: str,
	display_name: str,
	package_id: str,
	package_path: str,
	redirect_scheme: str,
	redirect_host: str,
) -> dict[Path, str]:
	main_activity_path = Path(
		f"mobile/android/app/src/main/java/{package_path}/MainActivity.kt"
	)
	native_bridge_path = Path(
		f"mobile/android/app/src/main/java/{package_path}/NativeBridge.kt"
	)

	return {
		Path("mobile/android/settings.gradle.kts"): _settings_gradle_template(app_name),
		Path("mobile/android/build.gradle.kts"): _root_build_gradle_template(),
		Path("mobile/android/.gitignore"): _android_gitignore_template(),
		Path("mobile/android/gradle.properties"): _gradle_properties_template(),
		Path("mobile/android/local.properties.example"): _local_properties_example_template(),
		Path("mobile/android/README.md"): _android_readme_template(app_name=app_name),
		Path("mobile/android/app/build.gradle.kts"): _app_build_gradle_template(package_id=package_id),
		Path("mobile/android/app/proguard-rules.pro"): _proguard_rules_template(),
		Path("mobile/android/app/src/main/AndroidManifest.xml"): _manifest_template(
			package_id=package_id,
			redirect_scheme=redirect_scheme,
			redirect_host=redirect_host,
		),
		Path("mobile/android/app/src/main/res/layout/activity_main.xml"): _activity_layout_template(),
		Path("mobile/android/app/src/main/res/values/strings.xml"): _strings_template(
			display_name=display_name
		),
		Path("mobile/app/index.html"): _standalone_index_template(display_name=display_name, app_name=app_name),
		Path("mobile/app/styles.css"): _standalone_styles_template(),
		Path("mobile/app/app.js"): _standalone_app_js_template(app_name=app_name),
		main_activity_path: _main_activity_template(
			package_id=package_id,
			redirect_scheme=redirect_scheme,
			redirect_host=redirect_host,
		),
		native_bridge_path: _native_bridge_template(package_id=package_id),
		Path("mobile/shared/config/environments.json"): _environments_template(),
		Path("mobile/shared/sdk/README.md"): _shared_sdk_readme_template(),
		Path("contracts/openapi/mobile-v1.yaml"): _openapi_template(),
		Path("docs/mobile-quickstart.md"): _quickstart_template(app_name=app_name),
	}


def _settings_gradle_template(app_name: str) -> str:
	return f"""import org.gradle.api.initialization.resolve.RepositoriesMode

pluginManagement {{
	repositories {{
		google()
		mavenCentral()
		gradlePluginPortal()
	}}
}}

dependencyResolutionManagement {{
	repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
	repositories {{
		google()
		mavenCentral()
	}}
}}

rootProject.name = "{app_name}_android"
include(":app")
"""


def _root_build_gradle_template() -> str:
	return """plugins {
	id("com.android.application") version "8.5.2" apply false
	id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
"""


def _android_gitignore_template() -> str:
	return """.gradle/
build/
local.properties
"""


def _gradle_properties_template() -> str:
	return """org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
android.useAndroidX=true
kotlin.code.style=official
android.nonTransitiveRClass=true
"""


def _local_properties_example_template() -> str:
	return """# Copy this file to local.properties and set your Android SDK location.
# Example on Linux:
# sdk.dir=/home/your-user/Android/Sdk
"""


def _android_readme_template(app_name: str) -> str:
	return f"""# Android Client ({app_name})

This folder is generated by:

```bash
bench native init --app {app_name} --platform android
```

If `gradlew` is missing, open this folder in Android Studio once, or run:

```bash
gradle wrapper --gradle-version 8.7
```
"""


def _app_build_gradle_template(package_id: str) -> str:
	return f"""plugins {{
	id("com.android.application")
	id("org.jetbrains.kotlin.android")
}}

android {{
	namespace = "{package_id}"
	compileSdk = 34

	defaultConfig {{
		applicationId = "{package_id}"
		minSdk = 24
		targetSdk = 34
		versionCode = 1
		versionName = "0.1.0"

		testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
	}}

	buildTypes {{
		release {{
			isMinifyEnabled = false
			proguardFiles(
				getDefaultProguardFile("proguard-android-optimize.txt"),
				"proguard-rules.pro"
			)
		}}
	}}

	compileOptions {{
		sourceCompatibility = JavaVersion.VERSION_17
		targetCompatibility = JavaVersion.VERSION_17
	}}

	kotlinOptions {{
		jvmTarget = "17"
	}}
}}

dependencies {{
	implementation("androidx.core:core-ktx:1.13.1")
	implementation("androidx.appcompat:appcompat:1.7.0")
	implementation("com.google.android.material:material:1.12.0")
	implementation("androidx.webkit:webkit:1.11.0")

	testImplementation("junit:junit:4.13.2")
	androidTestImplementation("androidx.test.ext:junit:1.2.1")
	androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}}
"""


def _proguard_rules_template() -> str:
	return """# Keep default rules for MVP.
"""


def _manifest_template(package_id: str, redirect_scheme: str, redirect_host: str) -> str:
	return f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
	<uses-permission android:name="android.permission.INTERNET" />

	<application
		android:allowBackup="true"
		android:label="@string/app_name"
		android:icon="@android:drawable/sym_def_app_icon"
		android:supportsRtl="true"
		android:usesCleartextTraffic="true"
		android:theme="@style/Theme.MaterialComponents.DayNight.NoActionBar">
		<activity
			android:name="{package_id}.MainActivity"
			android:launchMode="singleTask"
			android:exported="true">
			<intent-filter>
				<action android:name="android.intent.action.MAIN" />

				<category android:name="android.intent.category.LAUNCHER" />
			</intent-filter>
			<intent-filter>
				<action android:name="android.intent.action.VIEW" />
				<category android:name="android.intent.category.DEFAULT" />
				<category android:name="android.intent.category.BROWSABLE" />
				<data android:scheme="{redirect_scheme}" android:host="{redirect_host}" />
			</intent-filter>
		</activity>
	</application>

</manifest>
"""


def _activity_layout_template() -> str:
	return """<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
	android:layout_width="match_parent"
	android:layout_height="match_parent">

	<WebView
		android:id="@+id/web_view"
		android:layout_width="match_parent"
		android:layout_height="match_parent" />

</FrameLayout>
"""


def _strings_template(display_name: str) -> str:
	return f"""<?xml version="1.0" encoding="utf-8"?>
<resources>
	<string name="app_name">{display_name}</string>
</resources>
"""


def _standalone_index_template(display_name: str, app_name: str) -> str:
	return f"""<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1" />
	<title>{display_name}</title>
	<link rel="stylesheet" href="./styles.css" />
</head>
<body>
	<main class="card">
		<div class="badge">Standalone APK Screen</div>
		<h1>Welcome to Frappe Native</h1>
		<p>This is the default standalone start page bundled inside your APK.</p>
		<p>App: <strong>{display_name}</strong> (<code>{app_name}</code>)</p>
		<p>You can replace this file with Vue, React, or plain HTML/CSS/JS without requiring a live site URL.</p>
		<p>Edit source files in:</p>
		<div class="code">apps/{app_name}/mobile/app/</div>
		<div class="status" id="bridge-status">Checking NativeBridge...</div>
	</main>
	<script src="./app.js"></script>
</body>
</html>
"""


def _standalone_styles_template() -> str:
	return """:root {
	--bg: #f7f8fb;
	--card: #ffffff;
	--text: #15212e;
	--muted: #5a6777;
	--accent: #0078d4;
	--border: #dde3ea;
}
* { box-sizing: border-box; }
body {
	margin: 0;
	font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
	background: radial-gradient(circle at top right, #e8f2ff 0%, var(--bg) 45%);
	color: var(--text);
	min-height: 100vh;
	display: grid;
	place-items: center;
	padding: 24px;
}
.card {
	width: min(720px, 100%);
	background: var(--card);
	border: 1px solid var(--border);
	border-radius: 18px;
	padding: 28px;
	box-shadow: 0 10px 30px rgba(13, 23, 34, 0.08);
}
h1 {
	margin: 0 0 12px;
	font-size: 30px;
	letter-spacing: -0.02em;
}
p {
	margin: 0 0 12px;
	line-height: 1.55;
	color: var(--muted);
}
.badge {
	display: inline-block;
	padding: 6px 10px;
	border-radius: 999px;
	background: #e9f3ff;
	color: var(--accent);
	font-weight: 600;
	font-size: 12px;
	margin-bottom: 16px;
}
.code {
	font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	background: #f3f6fa;
	border: 1px solid var(--border);
	padding: 10px 12px;
	border-radius: 10px;
	color: #2b3440;
	font-size: 13px;
	overflow-x: auto;
	margin-bottom: 12px;
}
.status {
	margin-top: 8px;
	padding: 10px 12px;
	border-radius: 10px;
	background: #f4f6f8;
	border: 1px solid var(--border);
	color: #2b3440;
	font-size: 13px;
}
"""


def _standalone_app_js_template(app_name: str) -> str:
	return f"""(function () {{
	const el = document.getElementById("bridge-status");
	if (!el) return;

	if (window.NativeBridge && typeof window.NativeBridge.getDeviceInfo === "function") {{
		try {{
			const deviceInfo = JSON.parse(window.NativeBridge.getDeviceInfo());
			el.textContent = "NativeBridge connected (" + (deviceInfo.platform || "android") + ")";
		}} catch (error) {{
			el.textContent = "NativeBridge available, but device info parse failed";
		}}
	}} else {{
		el.textContent = "NativeBridge not available (web preview mode)";
	}}
}})();
"""


def _auth_config_js_template(
	app_name: str,
	site_url: str,
	bootstrap_method: str,
	redirect_uri: str,
	scope: str,
) -> str:
	config = {
		"native_app": app_name,
		"site_url": site_url,
		"bootstrap_method": bootstrap_method,
		"oauth": {
			"redirect_uri": redirect_uri,
			"scope": scope,
		},
	}
	return (
		"window.FrappeNativeAuthConfig = "
		+ json.dumps(config, indent=2)
		+ ";\n"
	)


def _auth_index_template(display_name: str) -> str:
	return f"""<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1" />
	<title>{display_name}</title>
	<link rel="stylesheet" href="./styles.css" />
</head>
<body>
	<main class="shell">
		<section class="card" id="screen-landing">
			<div class="badge">Frappe Native</div>
			<h1>{display_name}</h1>
			<p class="muted">Ship mobile apps from Bench with OAuth login, token refresh, and secure logout.</p>
			<div class="actions">
				<button class="btn btn-primary" id="btn-login" type="button">Login</button>
			</div>
		</section>

		<section class="card hidden" id="screen-home">
			<div class="badge badge-success">Authenticated</div>
			<h2>Home</h2>
			<p class="muted" id="user-line">Loading user...</p>
			<p class="meta" id="roles-line"></p>
			<p class="meta" id="counts-line"></p>
			<p class="meta" id="site-line"></p>
			<div class="actions">
				<button class="btn btn-secondary" id="btn-logout" type="button">Logout</button>
			</div>
		</section>

		<section class="status" id="status-line">Preparing app...</section>
	</main>
	<script src="./auth.config.js"></script>
	<script src="./app.js"></script>
</body>
</html>
"""


def _auth_styles_template() -> str:
	return """:root {
	--bg: #0e1726;
	--surface: #111d32;
	--surface-soft: #14233d;
	--text: #eaf1ff;
	--muted: #a7b9d9;
	--primary: #4d88ff;
	--primary-pressed: #2f71ff;
	--success: #16a34a;
	--border: #243857;
}
* { box-sizing: border-box; }
body {
	margin: 0;
	font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
	background:
		radial-gradient(circle at 20% 10%, rgba(77, 136, 255, 0.22), transparent 34%),
		radial-gradient(circle at 80% 90%, rgba(22, 163, 74, 0.18), transparent 32%),
		var(--bg);
	color: var(--text);
	min-height: 100vh;
	display: grid;
	place-items: center;
	padding: 20px;
}
.shell {
	width: min(560px, 100%);
	display: grid;
	gap: 14px;
}
.card {
	background: linear-gradient(180deg, var(--surface) 0%, var(--surface-soft) 100%);
	border: 1px solid var(--border);
	border-radius: 18px;
	padding: 24px;
	box-shadow: 0 12px 36px rgba(3, 10, 24, 0.45);
}
.hidden {
	display: none;
}
h1, h2 {
	margin: 0 0 10px;
	letter-spacing: -0.02em;
}
.muted {
	margin: 0 0 16px;
	color: var(--muted);
	line-height: 1.55;
}
.meta {
	margin: 0 0 16px;
	color: #c9d8f5;
	font-size: 14px;
}
.badge {
	display: inline-block;
	padding: 6px 10px;
	border-radius: 999px;
	font-size: 12px;
	font-weight: 700;
	text-transform: uppercase;
	letter-spacing: 0.06em;
	background: rgba(77, 136, 255, 0.2);
	color: #91b4ff;
	margin-bottom: 14px;
}
.badge-success {
	background: rgba(22, 163, 74, 0.2);
	color: #85f0ad;
}
.actions {
	display: flex;
	gap: 10px;
}
.btn {
	appearance: none;
	border: 1px solid transparent;
	border-radius: 12px;
	padding: 12px 16px;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
	transition: transform 0.06s ease, background 0.2s ease;
}
.btn:active {
	transform: translateY(1px);
}
.btn-primary {
	background: var(--primary);
	color: white;
}
.btn-primary:active {
	background: var(--primary-pressed);
}
.btn-secondary {
	background: transparent;
	border-color: var(--border);
	color: var(--text);
}
.status {
	background: rgba(17, 29, 50, 0.9);
	border: 1px solid var(--border);
	border-radius: 12px;
	padding: 10px 12px;
	font-size: 13px;
	color: var(--muted);
}
"""


def _auth_app_js_template() -> str:
	return """(function () {
	"use strict";

	const STORAGE_KEY = "frappe_native_tokens_v1";
	const DRAFT_KEY = "frappe_native_login_draft_v1";

	const els = {
		landing: document.getElementById("screen-landing"),
		home: document.getElementById("screen-home"),
		status: document.getElementById("status-line"),
		userLine: document.getElementById("user-line"),
		rolesLine: document.getElementById("roles-line"),
		countsLine: document.getElementById("counts-line"),
		siteLine: document.getElementById("site-line"),
		loginBtn: document.getElementById("btn-login"),
		logoutBtn: document.getElementById("btn-logout"),
	};

	let config = null;
	let bootstrap = null;

	async function main() {
		try {
			config = await loadConfig();
			wireEvents();
			setStatus("Ready");

			await handleOAuthCallback();

			const snapshot = await getSessionSnapshot();
			if (snapshot) {
				renderHome(snapshot);
			} else {
				renderLanding();
			}
		} catch (error) {
			setStatus(error.message || "Failed to initialize app");
			renderLanding();
		}
	}

	function wireEvents() {
		if (els.loginBtn) {
			els.loginBtn.addEventListener("click", startLogin);
		}
		if (els.logoutBtn) {
			els.logoutBtn.addEventListener("click", logout);
		}
	}

	async function loadConfig() {
		if (window.FrappeNativeAuthConfig && typeof window.FrappeNativeAuthConfig === "object") {
			return window.FrappeNativeAuthConfig;
		}
		throw new Error("Missing auth.config.js (window.FrappeNativeAuthConfig)");
	}

	function setStatus(message) {
		if (els.status) {
			els.status.textContent = message;
		}
	}

	function renderLanding() {
		if (els.landing) els.landing.classList.remove("hidden");
		if (els.home) els.home.classList.add("hidden");
	}

	function renderHome(snapshot) {
		if (els.home) els.home.classList.remove("hidden");
		if (els.landing) els.landing.classList.add("hidden");
		const user = snapshot.full_name
			? snapshot.full_name + " (" + snapshot.user + ")"
			: snapshot.user;
		if (els.userLine) {
			els.userLine.textContent = "Logged in as " + user;
		}
		if (els.rolesLine) {
			const roles = Array.isArray(snapshot.roles) && snapshot.roles.length
				? snapshot.roles.join(", ")
				: "No roles";
			els.rolesLine.textContent = "Roles: " + roles;
		}
		if (els.countsLine) {
			const counts = snapshot.counts || {};
			const todoCount = formatCount("ToDo", counts.ToDo);
			const userCount = formatCount("User", counts.User);
			els.countsLine.textContent = "Counts: " + todoCount + " | " + userCount;
		}
		if (els.siteLine) {
			els.siteLine.textContent = "Site: " + config.site_url;
		}
	}

	function formatCount(label, entry) {
		if (!entry || typeof entry !== "object") {
			return label + " n/a";
		}
		if (entry.status === "ok") {
			return label + " " + String(entry.value ?? 0);
		}
		if (entry.status === "no_access") {
			return label + " no-access";
		}
		return label + " error";
	}

	function unwrap(payload) {
		if (payload && typeof payload === "object" && "message" in payload) {
			return payload.message;
		}
		return payload;
	}

	function randomToken() {
		const bytes = new Uint8Array(32);
		crypto.getRandomValues(bytes);
		return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
	}

	function getDraft() {
		const raw = localStorage.getItem(DRAFT_KEY);
		if (!raw) return null;
		try {
			return JSON.parse(raw);
		} catch (error) {
			return null;
		}
	}

	function saveDraft(draft) {
		localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
	}

	function clearDraft() {
		localStorage.removeItem(DRAFT_KEY);
	}

	function getTokens() {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return null;
		try {
			return JSON.parse(raw);
		} catch (error) {
			return null;
		}
	}

	function saveTokens(tokenPayload) {
		const expiresAt = Date.now() + (Number(tokenPayload.expires_in) || 0) * 1000;
		const value = {
			access_token: tokenPayload.access_token,
			refresh_token: tokenPayload.refresh_token || null,
			expires_at: expiresAt,
		};
		localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
	}

	function clearTokens() {
		localStorage.removeItem(STORAGE_KEY);
	}

	async function getBootstrap() {
		if (bootstrap) return bootstrap;
		const appId = config.native_app || "";
		const query = new URLSearchParams({ app: appId }).toString();
		const url = config.site_url + "/api/method/" + config.bootstrap_method + "?" + query;
		const response = await fetch(url, { method: "GET" });
		const payload = await response.json();
		const message = unwrap(payload);
		if (!message || !message.client_id) {
			throw new Error("Bootstrap endpoint did not return client_id");
		}
		bootstrap = message;
		return bootstrap;
	}

	async function startLogin() {
		try {
			setStatus("Preparing login...");
			const info = await getBootstrap();
			const state = randomToken();
			const codeVerifier = randomToken();
			saveDraft({ state, code_verifier: codeVerifier, created_at: Date.now() });

			const redirectUri = info.redirect_uri || config.oauth.redirect_uri;
			const scope = info.scope || config.oauth.scope || "all openid";
			const params = new URLSearchParams({
				client_id: info.client_id,
				redirect_uri: redirectUri,
				response_type: "code",
				scope,
				state,
				code_challenge: codeVerifier,
				code_challenge_method: "plain",
			});
			window.location.href =
				config.site_url + "/api/method/frappe.integrations.oauth2.authorize?" + params.toString();
		} catch (error) {
			setStatus(error.message || "Failed to start login");
		}
	}

	async function handleOAuthCallback() {
		const current = new URL(window.location.href);
		const code = current.searchParams.get("code");
		const state = current.searchParams.get("state");
		const error = current.searchParams.get("error");
		const errorDescription = current.searchParams.get("error_description");

		if (!code && !error) {
			return;
		}

		if (error) {
			setStatus("Login failed: " + (errorDescription || error));
			clearAuthParams();
			return;
		}

		const draft = getDraft();
		if (!draft || draft.state !== state) {
			setStatus("Login failed: state mismatch");
			clearAuthParams();
			clearDraft();
			return;
		}

		const info = await getBootstrap();
		const redirectUri = info.redirect_uri || config.oauth.redirect_uri;
		const token = await exchangeCodeForToken({
			client_id: info.client_id,
			redirect_uri: redirectUri,
			code,
			code_verifier: draft.code_verifier,
		});
		saveTokens(token);
		clearDraft();
		clearAuthParams();
		setStatus("Login successful");
	}

	function clearAuthParams() {
		window.history.replaceState({}, "", "./index.html");
	}

	async function exchangeCodeForToken(payload) {
		const body = new URLSearchParams({
			grant_type: "authorization_code",
			client_id: payload.client_id,
			redirect_uri: payload.redirect_uri,
			code: payload.code,
			code_verifier: payload.code_verifier,
		});

		const response = await fetch(config.site_url + "/api/method/frappe.integrations.oauth2.get_token", {
			method: "POST",
			headers: { "Content-Type": "application/x-www-form-urlencoded" },
			body,
		});
		const data = await response.json();
		if (!response.ok || data.error || !data.access_token) {
			throw new Error(data.error_description || data.error || "Token exchange failed");
		}
		return data;
	}

	async function refreshTokenIfNeeded(tokens) {
		if (!tokens) return null;
		const refreshWindowMs = 60 * 1000;
		if (tokens.expires_at && Date.now() < tokens.expires_at - refreshWindowMs) {
			return tokens;
		}
		if (!tokens.refresh_token) {
			return null;
		}

		const info = await getBootstrap();
		const body = new URLSearchParams({
			grant_type: "refresh_token",
			client_id: info.client_id,
			refresh_token: tokens.refresh_token,
		});
		const response = await fetch(config.site_url + "/api/method/frappe.integrations.oauth2.get_token", {
			method: "POST",
			headers: { "Content-Type": "application/x-www-form-urlencoded" },
			body,
		});
		const data = await response.json();
		if (!response.ok || data.error || !data.access_token) {
			return null;
		}
		saveTokens(data);
		return getTokens();
	}

	async function getSessionSnapshot() {
		const tokens = await refreshTokenIfNeeded(getTokens());
		if (!tokens || !tokens.access_token) {
			clearTokens();
			return null;
		}

		const response = await fetch(
			config.site_url + "/api/method/frappe_native.api.mobile_auth.get_session_snapshot",
			{
			method: "GET",
			headers: {
				Authorization: "Bearer " + tokens.access_token,
			},
		});

		if (response.status === 401 || response.status === 403) {
			clearTokens();
			return null;
		}
		if (!response.ok) {
			setStatus("Failed to load session user");
			return null;
		}

		const payload = await response.json();
		const snapshot = unwrap(payload);
		if (!snapshot || !snapshot.user || snapshot.user === "Guest") {
			return null;
		}
		return snapshot;
	}

	async function revokeToken(token, tokenTypeHint) {
		if (!token) return;
		const body = new URLSearchParams({ token, token_type_hint: tokenTypeHint });
		try {
			await fetch(config.site_url + "/api/method/frappe.integrations.oauth2.revoke_token", {
				method: "POST",
				headers: { "Content-Type": "application/x-www-form-urlencoded" },
				body,
			});
		} catch (error) {
			// best effort
		}
	}

	async function logout() {
		const tokens = getTokens() || {};
		await revokeToken(tokens.access_token, "access_token");
		await revokeToken(tokens.refresh_token, "refresh_token");
		clearTokens();
		clearDraft();
		setStatus("Logged out");
		renderLanding();
	}

	main();
})();
"""


def _auth_quickstart_template(app_name: str, site: str, redirect_uri: str) -> str:
	return f"""# Mobile Auth Quickstart

This file was generated by:

```bash
bench native auth init --app {app_name} --site {site}
```

## Auth Flow Included

- Landing screen with **Login** button
- OAuth authorization code login with PKCE (plain challenge)
- Access token + refresh token storage
- Session snapshot fetch (user, roles, doctype counts)
- Logout with token revocation

## Files You Can Edit

- `mobile/app/index.html`
- `mobile/app/styles.css`
- `mobile/app/app.js`
- `mobile/app/auth.config.js`

## Redirect URI

`{redirect_uri}`

This URI is registered in OAuth Client and in Android deep-link intent filter.

## Bootstrap Endpoint

The mobile app fetches auth bootstrap from:

`/api/method/frappe_native.api.mobile_auth.get_client_id?app={app_name}`
"""


def _main_activity_template(package_id: str, redirect_scheme: str, redirect_host: str) -> str:
	return f"""package {package_id}

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {{
	private lateinit var webView: WebView
	private val startPage = "file:///android_asset/frappe_native/index.html"
	private val oauthRedirectScheme = "{redirect_scheme}"
	private val oauthRedirectHost = "{redirect_host}"

	@SuppressLint("SetJavaScriptEnabled")
	override fun onCreate(savedInstanceState: Bundle?) {{
		super.onCreate(savedInstanceState)
		setContentView(R.layout.activity_main)

		webView = findViewById(R.id.web_view)
		webView.settings.javaScriptEnabled = true
		webView.settings.domStorageEnabled = true
		webView.settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
		webView.settings.allowFileAccess = true
		webView.settings.allowContentAccess = true
		webView.settings.allowFileAccessFromFileURLs = true
		webView.settings.allowUniversalAccessFromFileURLs = true
		webView.setBackgroundColor(Color.WHITE)
		webView.webViewClient = object : WebViewClient() {{
			override fun shouldOverrideUrlLoading(
				view: WebView?,
				request: WebResourceRequest?,
			): Boolean {{
				val target = request?.url ?: return false
				val isOAuthCallback = target.scheme == oauthRedirectScheme && target.host == oauthRedirectHost
				if (!isOAuthCallback) {{
					return false
				}}

				startActivity(Intent(Intent.ACTION_VIEW, target))
				return true
			}}

			override fun onReceivedError(
				view: WebView?,
				request: WebResourceRequest?,
				error: WebResourceError?,
			) {{
				val target = request?.url
				val isOAuthCallback = target?.scheme == oauthRedirectScheme && target.host == oauthRedirectHost
				if (isOAuthCallback) {{
					// oauth callback is expected to leave WebView and reopen activity with intent data.
					return
				}}

				if (request?.isForMainFrame == true) {{
					showErrorPage("WebView load error: ${{error?.description ?: "unknown"}}")
				}}
			}}
		}}
		webView.webChromeClient = object : WebChromeClient() {{
			override fun onConsoleMessage(consoleMessage: ConsoleMessage): Boolean {{
				val message = "[${{consoleMessage.messageLevel()}}] " +
					"${{consoleMessage.message()}} " +
					"(${{consoleMessage.sourceId()}}:${{consoleMessage.lineNumber()}})"
				Log.d("FrappeNativeWebView", message)
				return true
			}}
		}}

		// Exposes Android-native methods to JS as `window.NativeBridge`.
		webView.addJavascriptInterface(NativeBridge(this), "NativeBridge")

		webView.loadUrl(resolveStartPage(intent))
	}}

	override fun onNewIntent(intent: Intent) {{
		super.onNewIntent(intent)
		setIntent(intent)
		webView.loadUrl(resolveStartPage(intent))
	}}

	private fun resolveStartPage(incomingIntent: Intent?): String {{
		val data: Uri = incomingIntent?.data ?: return startPage
		val isOAuthCallback = data.scheme == oauthRedirectScheme && data.host == oauthRedirectHost
		if (!isOAuthCallback) {{
			return startPage
		}}

		val pageUri = Uri.parse(startPage).buildUpon()
		data.getQueryParameter("code")?.let {{ pageUri.appendQueryParameter("code", it) }}
		data.getQueryParameter("state")?.let {{ pageUri.appendQueryParameter("state", it) }}
		data.getQueryParameter("error")?.let {{ pageUri.appendQueryParameter("error", it) }}
		data.getQueryParameter("error_description")?.let {{
			pageUri.appendQueryParameter("error_description", it)
		}}
		return pageUri.build().toString()
	}}

	private fun showErrorPage(message: String) {{
		val safeMessage = message
			.replace("&", "&amp;")
			.replace("<", "&lt;")
			.replace(">", "&gt;")
		val html = "<html><body style=\\"font-family: sans-serif; padding: 20px; background: #ffffff; color: #222222;\\">" +
			"<h2>Unable to load app</h2>" +
			"<p>" + safeMessage + "</p>" +
			"<p>Current page: <code>" + startPage + "</code></p>" +
			"<p>Edit <code>mobile/app/index.html</code>, then run <code>bench native build</code> to sync and rebuild.</p>" +
			"</body></html>"
		webView.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
	}}
}}
"""


def _native_bridge_template(package_id: str) -> str:
	return f"""package {package_id}

import android.content.Context
import android.os.Build
import android.webkit.JavascriptInterface
import org.json.JSONObject

class NativeBridge(private val context: Context) {{
	@JavascriptInterface
	fun getDeviceInfo(): String {{
		val payload = JSONObject()
		payload.put("platform", "android")
		payload.put("manufacturer", Build.MANUFACTURER)
		payload.put("model", Build.MODEL)
		payload.put("sdk_int", Build.VERSION.SDK_INT)
		return payload.toString()
	}}

	@JavascriptInterface
	fun pickFile(): String {{
		// Placeholder for file picker integration in next milestone.
		return JSONObject().put("status", "todo").put("feature", "pickFile").toString()
	}}

	@JavascriptInterface
	fun capturePhoto(): String {{
		// Placeholder for camera integration in next milestone.
		return JSONObject().put("status", "todo").put("feature", "capturePhoto").toString()
	}}
}}
"""


def _environments_template() -> str:
	return """{
	"dev": {
		"api_base_url": "http://10.0.2.2:8000",
		"socket_url": "ws://10.0.2.2:9000",
		"site_name": "dev.localhost"
	},
	"staging": {
		"api_base_url": "https://staging.example.com",
		"socket_url": "wss://staging.example.com/socket.io",
		"site_name": "staging.example.com"
	},
	"prod": {
		"api_base_url": "https://example.com",
		"socket_url": "wss://example.com/socket.io",
		"site_name": "example.com"
	}
}
"""


def _shared_sdk_readme_template() -> str:
	return """# Shared Mobile SDK

This folder is reserved for framework-agnostic client helpers that any UI stack can use:

- plain JavaScript
- Vue
- React
- HTML/CSS/JS

Planned MVP modules:

- API client wrapper (`fetch`)
- auth token store helpers
- request retry and error normalization
"""


def _openapi_template() -> str:
	return """openapi: 3.0.3
info:
  title: Native Mobile API
  version: 1.0.0
servers:
  - url: https://example.com
paths:
  /api/method/frappe_native.api.v1.health.ping:
    get:
      summary: Health ping
      responses:
        "200":
          description: Successful ping
  /api/method/frappe_native.api.v1.auth.login:
    post:
      summary: Login for native clients
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                email:
                  type: string
                password:
                  type: string
                device_id:
                  type: string
      responses:
        "200":
          description: Auth successful
"""


def _quickstart_template(app_name: str) -> str:
	return f"""# Mobile Quickstart

This project scaffold was generated for app: `{app_name}`.

## 1) Prepare Android tooling

Install Android Studio / Android SDK and ensure `gradle` is available.

Edit your standalone start page:

`mobile/app/index.html`

Optional supporting files:

`mobile/app/styles.css`

`mobile/app/app.js`

## 2) Run doctor checks

```bash
bench native doctor --app {app_name} --target android
```

## 3) Enable auth scaffold (landing/login/home/logout)

```bash
bench native auth init --app {app_name} --site <your-site>
```

## 4) Build debug APK

```bash
bench native build --app {app_name} --target android --variant debug
```

## 5) Build + Install + Launch in one command

```bash
bench native run --app {app_name} --target android --variant debug
bench native run --app {app_name} --target android --variant debug --logs
bench native run --app {app_name} --target android --variant debug --live --logs
```

If you still prefer manual Gradle:

```bash
cd apps/{app_name}/mobile/android
gradle wrapper --gradle-version 8.7
./gradlew assembleDebug
```
"""
