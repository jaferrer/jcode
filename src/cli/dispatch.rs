#![cfg_attr(test, allow(clippy::await_holding_lock))]

use anyhow::Result;
use std::io::IsTerminal;
use std::process::{Command as ProcessCommand, Stdio};
use std::time::Instant;

use super::args::{
    AmbientCommand, Args, AuthCommand, CloudCommand, CloudSessionsCommand, Command, MemoryCommand,
    ModelCommand, ProviderCommand, RestartCommand, ServerCommand, SessionCommand,
    TranscriptModeArg,
};
use crate::{
    agent, auth, build, provider, provider_catalog, server, session, setup_hints, startup_profile,
    tui,
};

use super::{
    account, acp, commands, debug, hot_exec, login, output, provider_init, selfdev, terminal,
    tui_launch,
};
use provider_init::ProviderChoice;

fn is_file_controlled_debug_client() -> bool {
    std::env::var_os("JCODE_DEBUG_CMD_PATH").is_some()
}

#[cfg(target_os = "linux")]
fn is_orphan_adopter_name(name: &str) -> bool {
    matches!(name.trim(), "init" | "systemd")
}

#[cfg(target_os = "linux")]
fn parent_is_orphan_adopter(parent_pid: libc::pid_t) -> bool {
    if parent_pid <= 1 {
        return true;
    }
    std::fs::read_to_string(format!("/proc/{parent_pid}/comm"))
        .is_ok_and(|name| is_orphan_adopter_name(&name))
}

/// Tie file-controlled debug clients to the process that launched them.
///
/// These clients are automation helpers, not user-owned terminals. Without a
/// parent-death signal they are reparented to init when a verification script
/// or debug server exits, retaining a full TUI and session history indefinitely.
#[cfg(target_os = "linux")]
fn arm_debug_client_parent_death_signal() {
    if !is_file_controlled_debug_client() {
        return;
    }

    // Capture the parent first, then check it again after prctl. This closes the
    // race where the launcher exits immediately before the signal is armed.
    // Safety: getppid has no preconditions and does not dereference pointers.
    let parent_pid = unsafe { libc::getppid() };
    // Safety: PR_SET_PDEATHSIG accepts a signal number as its scalar argument.
    let armed = unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) } == 0;
    // Safety: getppid has no preconditions and does not dereference pointers.
    let current_parent_pid = unsafe { libc::getppid() };
    if armed
        && (parent_is_orphan_adopter(parent_pid)
            || current_parent_pid != parent_pid
            || parent_is_orphan_adopter(current_parent_pid))
    {
        std::process::exit(0);
    }
}

#[cfg(not(target_os = "linux"))]
fn arm_debug_client_parent_death_signal() {}

pub(crate) async fn run_main(mut args: Args) -> Result<()> {
    arm_debug_client_parent_death_signal();
    resolve_resume_arg(&mut args)?;

    // One-time config migration: users whose config.toml still carries the old
    // baked-in `swarm_spawn_mode = "visible"` default get flipped to the
    // current `inline` default. Cheap (single file read, marker-gated), and it
    // must run before the config cache is first populated.
    crate::config::Config::migrate_legacy_swarm_spawn_mode_once();
    // One-time config migration: force idle_animation off for all existing
    // users; anyone re-enabling it afterwards keeps their choice.
    crate::config::Config::migrate_idle_animation_off_once();

    if args.omniroute {
        // Refresh the synced combo list first so `--omniroute` is
        // self-sufficient even when the shell wrapper
        // (omniroute-jcode-sync.sh) did not run before launch.
        sync_omniroute_combos().await;
        args.provider_profile = Some("omniroute".to_string());
    }

    if let Some(profile_name) = args
        .provider_profile
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        provider_catalog::apply_named_provider_profile_env(profile_name)?;
        crate::env::set_var("JCODE_PROVIDER_PROFILE_NAME", profile_name);
        crate::env::set_var("JCODE_PROVIDER_PROFILE_ACTIVE", "1");
        args.provider = ProviderChoice::OpenaiCompatible;

        if args.omniroute {
            // JCODE_OMNIROUTE_ONLY is the authoritative kill-switch for the
            // whole session: model_catalog_enabled() in
            // openrouter_provider_impl.rs and multiprovider_model_routes() in
            // catalog_routes.rs both check it, forcing every model list down
            // to the synced combos regardless of the profile's own
            // model_catalog=true and other configured providers.
            crate::env::set_var("JCODE_OMNIROUTE_ONLY", "1");
            let combo_ids: Vec<String> = crate::config::config()
                .providers
                .get(profile_name)
                .map(|profile| {
                    profile
                        .models
                        .iter()
                        .map(|model| model.id.trim().to_string())
                        .collect()
                })
                .unwrap_or_default();
            args.model = omniroute_model_override(args.model.as_deref(), &combo_ids);
        }
    }

    if let Some(tool_profile) = args.tool_profile.as_deref() {
        crate::env::set_var("JCODE_TOOL_PROFILE", tool_profile);
    }
    if let Some(tools) = args.tools.as_deref() {
        crate::env::set_var("JCODE_TOOLS", tools);
    }
    if let Some(disabled_tools) = args.disabled_tools.as_deref() {
        crate::env::set_var("JCODE_DISABLED_TOOLS", disabled_tools);
    }
    if args.disable_base_tools {
        crate::env::set_var("JCODE_DISABLE_BASE_TOOLS", "1");
    }
    if args.tool_profile.is_some()
        || args.tools.is_some()
        || args.disabled_tools.is_some()
        || args.disable_base_tools
    {
        crate::config::invalidate_config_cache();
    }

    match args.command {
        Some(Command::Serve {
            temporary_server,
            owner_pid,
            temp_idle_timeout_secs,
            server_name,
        }) => {
            let serve_start = Instant::now();
            crate::env::set_var("JCODE_NON_INTERACTIVE", "1");
            if temporary_server {
                server::configure_temporary_server(owner_pid, temp_idle_timeout_secs);
            }
            let provider_start = Instant::now();
            let provider =
                provider_init::init_provider(&args.provider, args.model.as_deref()).await?;
            let provider_ms = provider_start.elapsed().as_millis();
            let server_new_start = Instant::now();
            let server = server::Server::new_with_name(provider, server_name);
            let server_new_ms = server_new_start.elapsed().as_millis();
            crate::logging::info(&format!(
                "[TIMING] serve bootstrap: provider_init={}ms, server_new={}ms, before_run={}ms",
                provider_ms,
                server_new_ms,
                serve_start.elapsed().as_millis()
            ));
            server.run().await?;
        }
        Some(Command::Acp) => {
            acp::run_acp_command(
                args.provider,
                args.model.clone(),
                args.provider_profile.clone(),
                args.tool_profile.is_some(),
            )
            .await?;
        }
        Some(Command::Connect) => {
            tui_launch::run_client().await?;
        }
        #[cfg(unix)]
        Some(Command::ApiBridge { api_socket }) => {
            // The daemon must be up for the bridge to translate onto, and a
            // user running this to try the SDK has usually never started one.
            // Starting it here turns "connection refused, good luck" into a
            // working socket.
            //
            // Best-effort on purpose: a spawn can legitimately fail while a
            // usable daemon exists (another build already holds the runtime
            // dir, say). Aborting then would leave the SDK with no endpoint
            // over a daemon that was fine, so report and listen anyway. If the
            // daemon really is absent, per-client dials fail with a message
            // naming the socket, which is the smaller and more accurate error.
            if let Err(error) = spawn_server(
                &args.provider,
                args.model.as_deref(),
                args.provider_profile.as_deref(),
            )
            .await
            {
                eprintln!("api-bridge: could not start the jcode server: {error:#}");
                eprintln!("api-bridge: continuing; an already-running server will still be used");
            }
            let api_socket = api_socket
                .map(std::path::PathBuf::from)
                .unwrap_or_else(jcode_harness_api_server::api_socket_path);
            // The global `--socket` (and `JCODE_SOCKET`) already selects the
            // daemon socket; `set_socket_path` exported it during startup.
            let legacy_socket = jcode_harness_api_server::legacy_socket_path();
            jcode_harness_api_server::run_bridge(api_socket, legacy_socket).await?;
        }
        Some(Command::Server { action }) => match action {
            ServerCommand::Start { json } => {
                spawn_server(
                    &args.provider,
                    args.model.as_deref(),
                    args.provider_profile.as_deref(),
                )
                .await?;
                if json {
                    println!(
                        "{}",
                        serde_json::json!({
                            "status": "running",
                        })
                    );
                } else {
                    println!("Jcode server is running.");
                }
            }
            ServerCommand::Keepalive => {
                run_server_keepalive(
                    &args.provider,
                    args.model.as_deref(),
                    args.provider_profile.as_deref(),
                )
                .await?;
            }
            ServerCommand::Promote { version, json } => {
                commands::run_server_promote_command(version.as_deref(), json)?;
            }
            ServerCommand::Reload { force, json } => {
                commands::run_server_reload_command(force, json).await?;
            }
            ServerCommand::Stop { force, json } => {
                commands::run_server_stop_command(force, json).await?;
            }
        },
        Some(Command::Run {
            message,
            json,
            ndjson,
        }) => {
            commands::run_single_message_command(
                &args.provider,
                args.model.as_deref(),
                args.resume.as_deref(),
                &message,
                json,
                ndjson,
            )
            .await?;
        }
        Some(Command::Login {
            provider: login_provider,
            account,
            no_browser,
            print_auth_url,
            callback_url,
            auth_code,
            json,
            complete,
            no_validate,
            google_access_tier,
            api_base,
            api_key,
            api_key_env,
        }) => {
            login::run_login(
                &login_provider.unwrap_or(args.provider),
                account.as_deref(),
                login::LoginOptions {
                    no_browser,
                    print_auth_url,
                    callback_url,
                    auth_code,
                    json,
                    complete,
                    no_validate,
                    google_access_tier: google_access_tier.map(|tier| match tier {
                        super::args::GoogleAccessTierArg::Full => {
                            auth::google::GmailAccessTier::Full
                        }
                        super::args::GoogleAccessTierArg::Readonly => {
                            auth::google::GmailAccessTier::ReadOnly
                        }
                    }),
                    openai_compatible_api_base: api_base,
                    openai_compatible_api_key: api_key,
                    openai_compatible_api_key_env: api_key_env,
                    openai_compatible_default_model: args.model.clone(),
                },
            )
            .await?;
        }
        Some(Command::Account { action }) => match action {
            super::args::AccountCommand::Login { no_browser } => {
                account::run_login(no_browser).await?
            }
            super::args::AccountCommand::Status { json } => account::run_status(json).await?,
            super::args::AccountCommand::Manage => account::run_manage()?,
            super::args::AccountCommand::Logout => account::run_logout().await?,
        },
        Some(Command::Repl) => {
            let (provider, registry) =
                provider_init::init_provider_and_registry(&args.provider, args.model.as_deref())
                    .await?;
            let mut agent = agent::Agent::new(provider, registry);
            agent.repl().await?;
        }
        Some(Command::Update) => {
            hot_exec::run_update()?;
        }
        Some(Command::Version { json }) => {
            commands::run_version_command(json)?;
        }
        Some(Command::Usage { json }) => {
            commands::run_usage_command(json).await?;
        }
        Some(Command::Telemetry(action)) => super::telemetry::run(action)?,
        Some(Command::SelfDev { build }) => {
            selfdev::run_self_dev(build, args.resume).await?;
        }
        Some(Command::Debug {
            command,
            arg,
            session,
            socket,
            wait,
        }) => {
            debug::run_debug_command(&command, &arg, session, socket, wait).await?;
        }
        Some(Command::Auth(subcmd)) => match subcmd {
            AuthCommand::Status { json } => commands::run_auth_status_command(json)?,
            AuthCommand::Doctor {
                provider,
                validate,
                json,
            } => {
                let provider_arg = auth_doctor_provider_arg(provider.as_deref(), &args.provider);
                commands::run_auth_doctor_command(provider_arg, validate, json).await?
            }
        },
        Some(Command::Provider(subcmd)) => match subcmd {
            ProviderCommand::List { json } => {
                commands::run_provider_list_command(json)?;
            }
            ProviderCommand::Current { json } => {
                commands::run_provider_current_command(&args.provider, args.model.as_deref(), json)
                    .await?;
            }
            ProviderCommand::Add {
                name,
                base_url,
                model,
                context_window,
                api_key_env,
                api_key,
                api_key_stdin,
                no_api_key,
                auth,
                auth_header,
                env_file,
                set_default,
                overwrite,
                provider_routing,
                model_catalog,
                json,
            } => {
                commands::run_provider_add_command(commands::ProviderAddOptions {
                    name,
                    base_url,
                    model,
                    context_window,
                    api_key_env,
                    api_key,
                    api_key_stdin,
                    no_api_key,
                    auth,
                    auth_header,
                    env_file,
                    set_default,
                    overwrite,
                    provider_routing,
                    model_catalog,
                    json,
                })?;
            }
        },
        Some(Command::Memory(subcmd)) => {
            commands::run_memory_command(map_memory_subcommand(subcmd))?;
        }
        Some(Command::Session(subcmd)) => match subcmd {
            SessionCommand::Rename {
                session,
                name,
                clear,
                json,
            } => commands::run_session_rename_command(&session, name.as_deref(), clear, json)?,
        },
        Some(Command::Ambient(subcmd)) => {
            commands::run_ambient_command(map_ambient_subcommand(subcmd)).await?;
        }
        Some(Command::Cloud(subcmd)) => {
            commands::run_cloud_command(map_cloud_subcommand(subcmd))?;
        }
        Some(Command::Pair { list, revoke }) => {
            commands::run_pair_command(list, revoke)?;
        }
        Some(Command::Permissions) => {
            tui::permissions::run_permissions()?;
        }
        Some(Command::Transcript {
            text,
            mode,
            session,
        }) => {
            commands::run_transcript_command(text, map_transcript_mode(mode), session).await?;
        }
        Some(Command::Dictate { r#type }) => {
            commands::run_dictate_command(r#type).await?;
        }
        Some(Command::SetupHotkey {
            listen_macos_hotkey,
            notify_cli_launch,
            listen_windows_hotkey,
            uninstall,
        }) => {
            setup_hints::run_setup_hotkey(
                listen_macos_hotkey,
                listen_windows_hotkey,
                uninstall,
                notify_cli_launch.as_deref(),
            )?;
        }
        Some(Command::SetupLauncher) => {
            setup_hints::run_setup_launcher()?;
        }
        Some(Command::Browser { action }) => {
            commands::run_browser(&action).await?;
        }
        Some(Command::Replay {
            session,
            swarm,
            export,
            speed,
            timeline,
            auto_edit,
            video,
            cols,
            rows,
            fps,
            centered,
            no_centered,
        }) => {
            let centered_override = if centered {
                Some(true)
            } else if no_centered {
                Some(false)
            } else {
                None
            };
            tui_launch::run_replay_command(
                &session,
                swarm,
                export,
                auto_edit,
                speed,
                timeline.as_deref(),
                video.as_deref(),
                cols,
                rows,
                fps,
                centered_override,
            )
            .await?;
        }
        Some(Command::Model(subcmd)) => match subcmd {
            ModelCommand::List { json, verbose } => {
                commands::run_model_command(&args.provider, args.model.as_deref(), json, verbose)
                    .await?;
            }
        },
        Some(Command::ProviderTestCoverage {
            provider_query,
            model_query,
            coverage_file,
            coverage_limit,
        }) => {
            let coverage_path = coverage_file.as_deref().map(std::path::Path::new);
            let colorize = std::io::stdout().is_terminal()
                && std::env::var_os("NO_COLOR").is_none()
                && std::env::var_os("JCODE_NO_COLOR").is_none();
            if let Some(provider) = provider_query {
                let model = model_query
                    .or_else(|| args.model.clone())
                    .unwrap_or_else(|| "*".to_string());
                let report = crate::live_tests::format_provider_test_coverage_report(
                    &provider,
                    &model,
                    coverage_path,
                );
                print_provider_test_coverage_report(&report, colorize);
            } else {
                let (coverage, path) = crate::live_tests::load_coverage(coverage_path)?;
                let summary = crate::live_tests::strict_live_provider_model_coverage_summary(
                    &coverage,
                    path.display().to_string(),
                );
                let report = crate::live_tests::format_strict_live_provider_model_coverage_summary(
                    &summary,
                    coverage_limit,
                );
                print_provider_test_coverage_report(&report, colorize);
            }
        }
        Some(Command::ProviderDoctor {
            provider,
            tier,
            json,
        }) => {
            crate::cli::provider_doctor::run_provider_doctor_command(
                &provider,
                args.model.as_deref(),
                &tier,
                json,
            )
            .await?;
        }
        Some(Command::AuthTest {
            login,
            all_configured,
            no_smoke,
            no_tool_smoke,
            prompt,
            json,
            output,
            coverage,
            context_audit,
            coverage_file,
            coverage_limit,
        }) => {
            if coverage {
                commands::run_auth_test_coverage_command(
                    json,
                    output.as_deref(),
                    coverage_file.as_deref(),
                    coverage_limit,
                )?;
            } else if context_audit {
                commands::run_auth_test_context_audit_command(
                    &args.provider,
                    all_configured,
                    json,
                    output.as_deref(),
                )
                .await?;
            } else {
                commands::run_auth_test_command(
                    &args.provider,
                    args.model.as_deref(),
                    login,
                    all_configured,
                    no_smoke,
                    no_tool_smoke,
                    prompt.as_deref(),
                    json,
                    output.as_deref(),
                )
                .await?;
            }
        }
        Some(Command::Restart { action }) => match action {
            RestartCommand::Save { auto_restore } => {
                commands::run_restart_save_command(auto_restore).await?
            }
            RestartCommand::Restore => commands::run_restart_restore_command()?,
            RestartCommand::Status => commands::run_restart_status_command()?,
            RestartCommand::Clear => commands::run_restart_clear_command()?,
        },
        Some(Command::Menubar { once, json }) => {
            commands::run_menubar_command(once, json)?;
        }
        None => run_default_command(args).await?,
    }

    Ok(())
}

/// Validates `--model` against `[providers.omniroute].models` (the combo IDs
/// synced by `omniroute-jcode-sync.py`) when `--omniroute` is active. Returns
/// `None` (falling back to the profile's `default_model`) and logs a warning
/// when the requested model isn't a synced combo.
fn omniroute_model_override(requested: Option<&str>, combo_ids: &[String]) -> Option<String> {
    let requested = requested?.trim();
    if requested.is_empty() {
        return None;
    }
    if combo_ids.iter().any(|id| id.trim() == requested) {
        return Some(requested.to_string());
    }
    crate::logging::warn(&format!(
        "--omniroute: '{requested}' is not a synced OmniRoute combo; falling back to the profile's default_model. Run the combo sync script or pick one from /model."
    ));
    None
}

/// Refreshes the `[[providers.omniroute.models]]` block in config.toml from
/// OmniRoute's live combo list (same rules as `omniroute-jcode-sync.py`):
/// `owned_by == "combo"`, slash-free ids, picker grouped by upstream provider.
/// Best-effort: any failure is logged and startup continues with the combos
/// already present in config.toml.
async fn sync_omniroute_combos() {
    match sync_omniroute_combos_inner().await {
        Ok(true) => {
            crate::logging::info("--omniroute: synced combo list from OmniRoute into config.toml")
        }
        Ok(false) => {}
        Err(err) => crate::logging::info(&format!(
            "--omniroute: combo sync skipped ({err:#}); using combos already in config.toml"
        )),
    }
}

async fn sync_omniroute_combos_inner() -> Result<bool> {
    use anyhow::Context;

    let config_path = crate::config::Config::path().context("no config.toml path")?;
    let content = std::fs::read_to_string(&config_path)?;
    if !content.lines().any(|l| l.trim() == "[providers.omniroute]") {
        return Ok(false);
    }

    // The profile's own base_url is the source of truth jcode will actually
    // talk to; OMNIROUTE_BASE_URL and localhost:20128 are fallbacks matching
    // the shell script.
    let base_url = crate::config::config()
        .providers
        .get("omniroute")
        .map(|profile| profile.base_url.trim().trim_end_matches('/').to_string())
        .filter(|url| !url.is_empty())
        .or_else(|| {
            std::env::var("OMNIROUTE_BASE_URL")
                .ok()
                .map(|url| url.trim().trim_end_matches('/').to_string())
        })
        .filter(|url| !url.is_empty())
        .unwrap_or_else(|| "http://localhost:20128".to_string());
    let url = if base_url.ends_with("/v1") {
        format!("{base_url}/models")
    } else {
        format!("{base_url}/v1/models")
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()?;
    let json: serde_json::Value = client.get(&url).send().await?.json().await?;
    let combos = parse_omniroute_combos(&json);

    let Some(new_content) = rewrite_omniroute_models_block(&content, &combos) else {
        return Ok(false);
    };
    std::fs::write(&config_path, new_content)?;
    crate::config::invalidate_config_cache();
    Ok(true)
}

/// Picker grouping for combos, ported from `omniroute-jcode-sync.py`:
/// copilot-* first, then *-wa / fable / opus / sonnet, kimi, qoder-, rest.
fn combo_sort_key(id: &str) -> (u8, String) {
    let name = id.to_lowercase();
    let group = if name.starts_with("copilot-") {
        0
    } else if name.ends_with("-wa")
        || name.starts_with("fable")
        || name.starts_with("opus")
        || name.starts_with("sonnet")
    {
        1
    } else if name.starts_with("kimi") {
        2
    } else if name.starts_with("qoder-") {
        3
    } else {
        4
    };
    (group, name)
}

/// Extracts named combos from a `/v1/models` payload: `owned_by == "combo"`
/// and slash-free ids (slash ids are OmniRoute's built-in generic `auto/*`
/// routers, not user-configured combos). The advertised context window comes
/// from `context_length` or `max_input_tokens`.
fn parse_omniroute_combos(json: &serde_json::Value) -> Vec<(String, Option<usize>)> {
    let mut combos: Vec<(String, Option<usize>)> = json
        .get("data")
        .and_then(|data| data.as_array())
        .map(|models| {
            models
                .iter()
                .filter_map(|model| {
                    if model.get("owned_by").and_then(|v| v.as_str()) != Some("combo") {
                        return None;
                    }
                    let id = model.get("id").and_then(|v| v.as_str())?.trim().to_string();
                    if id.is_empty() || id.contains('/') {
                        return None;
                    }
                    let window = ["context_length", "max_input_tokens"]
                        .iter()
                        .find_map(|key| {
                            model
                                .get(key)
                                .and_then(|v| v.as_u64())
                                .filter(|n| *n > 0)
                                .map(|n| n as usize)
                        });
                    Some((id, window))
                })
                .collect()
        })
        .unwrap_or_default();
    combos.sort_by(|a, b| combo_sort_key(&a.0).cmp(&combo_sort_key(&b.0)));
    combos
}

fn toml_basic_string(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{escaped}\"")
}

/// Replaces the `[[providers.omniroute.models]]` block of the omniroute
/// provider section with `combos`. Returns `None` when the section is
/// missing, `combos` is empty, or the block already matches (no write, so the
/// config fingerprint stays untouched).
fn rewrite_omniroute_models_block(
    content: &str,
    combos: &[(String, Option<usize>)],
) -> Option<String> {
    if combos.is_empty() {
        return None;
    }
    let lines: Vec<&str> = content.split('\n').collect();

    let mut start = None;
    for (i, line) in lines.iter().enumerate() {
        if line.trim() == "[[providers.omniroute.models]]" {
            for j in (0..=i).rev() {
                if lines[j].starts_with("[providers.") {
                    if lines[j].trim() == "[providers.omniroute]" {
                        start = Some(i);
                    }
                    break;
                }
            }
            if start.is_some() {
                break;
            }
        }
    }
    let start = start?;

    let mut end = lines.len();
    for (i, line) in lines.iter().enumerate().skip(start + 1) {
        if line.starts_with('[') && line.trim() != "[[providers.omniroute.models]]" {
            end = i;
            break;
        }
    }

    let mut current: Vec<(String, Option<usize>)> = Vec::new();
    for line in &lines[start..end] {
        let stripped = line.trim();
        if let Some(rest) = stripped.strip_prefix("id =") {
            if let Ok(id) = serde_json::from_str::<String>(rest.trim()) {
                current.push((id, None));
            }
        } else if let Some(rest) = stripped.strip_prefix("context_window =") {
            if let Some(last) = current.last_mut() {
                if let Ok(window) = rest.trim().parse::<usize>() {
                    last.1 = Some(window);
                }
            }
        }
    }
    if current == combos {
        return None;
    }

    let mut block: Vec<String> = Vec::new();
    for (id, window) in combos {
        block.push("[[providers.omniroute.models]]".to_string());
        block.push(format!("id = {}", toml_basic_string(id)));
        if let Some(window) = window {
            block.push(format!("context_window = {window}"));
        }
        block.push(String::new());
    }

    let mut new_lines: Vec<String> = Vec::with_capacity(lines.len() + block.len());
    new_lines.extend(lines[..start].iter().map(|line| line.to_string()));
    new_lines.extend(block);
    new_lines.extend(lines[end..].iter().map(|line| line.to_string()));
    Some(new_lines.join("\n"))
}

#[cfg(test)]
mod omniroute_flag_tests {
    use super::{
        omniroute_model_override, parse_omniroute_combos, rewrite_omniroute_models_block,
    };

    #[test]
    fn valid_combo_is_kept() {
        let combos = vec!["Sonnet5-WA".to_string(), "Opus5-WA".to_string()];
        assert_eq!(
            omniroute_model_override(Some("Sonnet5-WA"), &combos),
            Some("Sonnet5-WA".to_string())
        );
    }

    #[test]
    fn unknown_model_falls_back_to_default_model() {
        let combos = vec!["Sonnet5-WA".to_string()];
        assert_eq!(
            omniroute_model_override(Some("gpt-4-made-up"), &combos),
            None
        );
    }

    #[test]
    fn empty_or_missing_request_falls_back_to_default_model() {
        let combos = vec!["Sonnet5-WA".to_string()];
        assert_eq!(omniroute_model_override(Some("  "), &combos), None);
        assert_eq!(omniroute_model_override(None, &combos), None);
    }

    #[test]
    fn parse_combos_filters_named_combos_only() {
        let json = serde_json::json!({
            "data": [
                {"id": "auto/best-coding", "owned_by": "combo"},
                {"id": "Fable5-WA", "owned_by": "combo", "context_length": 1000000},
                {"id": "kimi-k2", "owned_by": "combo", "max_input_tokens": 256000},
                {"id": "gpt-5.5", "owned_by": "openai"},
                {"id": "qoder-plus", "owned_by": "combo"},
                {"id": "copilot-claude", "owned_by": "combo"}
            ]
        });
        let combos = parse_omniroute_combos(&json);
        assert_eq!(
            combos,
            vec![
                ("copilot-claude".to_string(), None),
                ("Fable5-WA".to_string(), Some(1000000)),
                ("kimi-k2".to_string(), Some(256000)),
                ("qoder-plus".to_string(), None),
            ]
        );
    }

    #[test]
    fn rewrite_replaces_block_and_preserves_rest() {
        let content = "[providers.omniroute]\nbase_url = \"http://localhost:20128/v1\"\n\n[[providers.omniroute.models]]\nid = \"Old-WA\"\n\n[providers.other]\nbase_url = \"http://x\"\n";
        let combos = vec![("Fable5-WA".to_string(), Some(1000000))];
        let out = rewrite_omniroute_models_block(content, &combos).unwrap();
        assert_eq!(
            out,
            "[providers.omniroute]\nbase_url = \"http://localhost:20128/v1\"\n\n[[providers.omniroute.models]]\nid = \"Fable5-WA\"\ncontext_window = 1000000\n\n[providers.other]\nbase_url = \"http://x\"\n"
        );
    }

    #[test]
    fn rewrite_is_noop_when_unchanged() {
        let content = "[providers.omniroute]\n\n[[providers.omniroute.models]]\nid = \"Fable5-WA\"\ncontext_window = 1000000\n\n";
        let combos = vec![("Fable5-WA".to_string(), Some(1000000))];
        assert_eq!(rewrite_omniroute_models_block(content, &combos), None);
    }

    #[test]
    fn rewrite_returns_none_without_omniroute_section() {
        let content = "[providers.other]\nbase_url = \"http://x\"\n";
        let combos = vec![("Fable5-WA".to_string(), None)];
        assert_eq!(rewrite_omniroute_models_block(content, &combos), None);
        assert_eq!(rewrite_omniroute_models_block(content, &[]), None);
    }
}

fn auth_doctor_provider_arg<'a>(
    positional_provider: Option<&'a str>,
    global_provider: &'a ProviderChoice,
) -> Option<&'a str> {
    positional_provider.or_else(|| {
        if *global_provider == ProviderChoice::Auto {
            None
        } else {
            Some(global_provider.as_arg_value())
        }
    })
}

fn resolve_resume_arg(args: &mut Args) -> Result<()> {
    if let Some(ref resume_id) = args.resume {
        if resume_id.is_empty() {
            return tui_launch::list_sessions();
        }

        let resume_id = resume_id.clone();
        match resolve_resume_id(&resume_id) {
            Ok(full_id) => {
                args.resume = Some(full_id);
            }
            Err(e) => {
                match resume_resolution_failure_action(&resume_id, |key| std::env::var_os(key)) {
                    // During a reload/update/restart handoff the client re-execs
                    // itself with `--resume <id>` and `JCODE_RESUMING=1`. In the
                    // client/server architecture the shared server is the authority
                    // for session lifecycle, so an id that is not in the local store
                    // can still be valid server-side. Hard-exiting here dumped the
                    // user back to a shell with "No session found matching ...",
                    // making jcode unusable after an auto-update (issue #328).
                    // Instead, keep the raw id and let the remote connection resolve
                    // it; if the server cannot find it either, the TUI surfaces a
                    // recoverable message and falls back to a fresh session rather
                    // than killing the process.
                    ResumeResolutionFailureAction::DeferToServer => {
                        crate::logging::warn(&format!(
                            "Resume id '{}' not found locally during reload handoff ({}); deferring resolution to the server instead of exiting",
                            resume_id, e
                        ));
                        // Leave args.resume as the raw id for the server to resolve.
                    }
                    ResumeResolutionFailureAction::Exit => {
                        eprintln!("Error: {}", e);
                        if !output::quiet_enabled() {
                            eprintln!("\nUse `jcode --resume` to list available sessions.");
                        }
                        std::process::exit(1);
                    }
                }
            }
        }
    }

    Ok(())
}

/// What to do when a `--resume <id>` cannot be resolved from the local session
/// store. Extracted as a pure function so the reload-handoff recovery path can
/// be unit-tested without invoking `std::process::exit` (issue #328).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ResumeResolutionFailureAction {
    /// Keep the raw id and let the shared server resolve it (reload handoff).
    DeferToServer,
    /// No live handoff in progress; the id is genuinely bad, so exit.
    Exit,
}

fn resume_resolution_failure_action<F, V>(
    _resume_id: &str,
    var_os: F,
) -> ResumeResolutionFailureAction
where
    F: Fn(&str) -> Option<V>,
{
    if var_os("JCODE_RESUMING").is_some() {
        ResumeResolutionFailureAction::DeferToServer
    } else {
        ResumeResolutionFailureAction::Exit
    }
}

fn resolve_resume_id(resume_id: &str) -> Result<String> {
    match session::find_session_by_name_or_id(resume_id) {
        Ok(full_id) => Ok(full_id),
        Err(native_err) => match crate::import::import_external_resume_id(resume_id)? {
            Some(imported_id) => Ok(imported_id),
            None => Err(native_err),
        },
    }
}

fn map_memory_subcommand(subcmd: MemoryCommand) -> commands::MemorySubcommand {
    match subcmd {
        MemoryCommand::List { scope, tag } => commands::MemorySubcommand::List { scope, tag },
        MemoryCommand::Search { query, semantic } => {
            commands::MemorySubcommand::Search { query, semantic }
        }
        MemoryCommand::Export { output, scope } => {
            commands::MemorySubcommand::Export { output, scope }
        }
        MemoryCommand::Import {
            input,
            scope,
            overwrite,
        } => commands::MemorySubcommand::Import {
            input,
            scope,
            overwrite,
        },
        MemoryCommand::Stats => commands::MemorySubcommand::Stats,
        MemoryCommand::ClearTest => commands::MemorySubcommand::ClearTest,
    }
}

fn map_ambient_subcommand(subcmd: AmbientCommand) -> commands::AmbientSubcommand {
    match subcmd {
        AmbientCommand::Status => commands::AmbientSubcommand::Status,
        AmbientCommand::Log => commands::AmbientSubcommand::Log,
        AmbientCommand::Trigger => commands::AmbientSubcommand::Trigger,
        AmbientCommand::Stop => commands::AmbientSubcommand::Stop,
        AmbientCommand::RunVisible => commands::AmbientSubcommand::RunVisible,
    }
}

fn map_cloud_subcommand(subcmd: CloudCommand) -> commands::CloudSubcommand {
    match subcmd {
        CloudCommand::Sessions { action } => {
            commands::CloudSubcommand::Sessions(map_cloud_sessions_subcommand(action))
        }
    }
}

fn map_cloud_sessions_subcommand(
    action: CloudSessionsCommand,
) -> commands::CloudSessionsSubcommand {
    match action {
        CloudSessionsCommand::Configure {
            api_base,
            api_token,
            api_token_env,
            api_token_id,
            user_id,
            helper,
            clear,
        } => commands::CloudSessionsSubcommand::Configure {
            api_base,
            api_token,
            api_token_env,
            api_token_id,
            user_id,
            helper,
            clear,
        },
        CloudSessionsCommand::Status { json } => commands::CloudSessionsSubcommand::Status { json },
        CloudSessionsCommand::Upload {
            session_file,
            raw,
            jade,
        } => commands::CloudSessionsSubcommand::Upload {
            session_file,
            raw,
            user_id: jade.user_id,
            profile: jade.profile,
            region: jade.region,
            helper: jade.helper,
        },
        CloudSessionsCommand::UploadLatest {
            sessions_dir,
            raw,
            jade,
        } => commands::CloudSessionsSubcommand::UploadLatest {
            sessions_dir,
            raw,
            user_id: jade.user_id,
            profile: jade.profile,
            region: jade.region,
            helper: jade.helper,
        },
        CloudSessionsCommand::Sync {
            sessions_dir,
            since_days,
            all,
            max,
            min_interval_mins,
            raw,
            dry_run,
            force,
            json,
            jade,
        } => commands::CloudSessionsSubcommand::Sync {
            sessions_dir,
            since_days,
            all,
            max,
            min_interval_mins,
            raw,
            dry_run,
            force,
            json,
            user_id: jade.user_id,
            profile: jade.profile,
            region: jade.region,
            helper: jade.helper,
        },
        CloudSessionsCommand::List { limit, json, jade } => {
            commands::CloudSessionsSubcommand::List {
                limit,
                json,
                user_id: jade.user_id,
                profile: jade.profile,
                region: jade.region,
                helper: jade.helper,
            }
        }
        CloudSessionsCommand::Verify { session_id, jade } => {
            commands::CloudSessionsSubcommand::Verify {
                session_id,
                user_id: jade.user_id,
                profile: jade.profile,
                region: jade.region,
                helper: jade.helper,
            }
        }
        CloudSessionsCommand::Dashboard {
            limit,
            output,
            open,
            with_view,
            jade,
        } => commands::CloudSessionsSubcommand::Dashboard {
            limit,
            output,
            open,
            with_view,
            user_id: jade.user_id,
            profile: jade.profile,
            region: jade.region,
            helper: jade.helper,
        },
        CloudSessionsCommand::View {
            session_id,
            format,
            output,
            open,
            jade,
        } => commands::CloudSessionsSubcommand::View {
            session_id,
            format: format.as_arg().to_string(),
            output,
            open,
            user_id: jade.user_id,
            profile: jade.profile,
            region: jade.region,
            helper: jade.helper,
        },
    }
}

fn map_transcript_mode(mode: TranscriptModeArg) -> crate::protocol::TranscriptMode {
    match mode {
        TranscriptModeArg::Insert => crate::protocol::TranscriptMode::Insert,
        TranscriptModeArg::Append => crate::protocol::TranscriptMode::Append,
        TranscriptModeArg::Replace => crate::protocol::TranscriptMode::Replace,
        TranscriptModeArg::Send => crate::protocol::TranscriptMode::Send,
    }
}

async fn run_default_command(args: Args) -> Result<()> {
    startup_profile::mark("run_main_none_branch");

    let explicit_provider_or_model = args.provider != ProviderChoice::Auto
        || args.model.is_some()
        || args.provider_profile.is_some();
    let explicit_tool_options = args.tool_profile.is_some()
        || args.tools.is_some()
        || args.disabled_tools.is_some()
        || args.disable_base_tools;
    if args.resume.is_none()
        && !explicit_provider_or_model
        && !explicit_tool_options
        && commands::maybe_run_pending_restart_restore_on_startup().await?
    {
        return Ok(());
    }

    let startup_hints = if args.fresh_spawn {
        None
    } else {
        // One-time: bake per-repo launch hotkeys from session history into config,
        // then reinstall so the new chords take effect. Scanning session history
        // can take a few hundred ms, so run it on a detached thread to keep it off
        // the first-frame critical path. It is gated by an `imported` flag, so it
        // does real work at most once and no-ops on every later launch.
        if std::io::IsTerminal::is_terminal(&std::io::stdin()) {
            std::thread::Builder::new()
                .name("launch-hotkey-bake".to_string())
                .spawn(|| {
                    if crate::config::Config::bake_launch_hotkeys_once() {
                        setup_hints::reinstall_launch_hotkeys_after_config_change();
                    }
                })
                .ok();
        }

        // Prefer existing setup hints (alignment/welcome/terminal nudges); only
        // surface the keybinding-conflict heads-up when nothing else is queued,
        // so we never clobber an early-launch tip. The conflict hint is
        // self-debouncing (shown once per distinct conflict set).
        setup_hints::maybe_show_setup_hints()
            .or_else(|| {
                setup_hints::maybe_show_keymap_conflict_hint(&crate::config::config().keybindings)
            })
            .or_else(setup_hints::maybe_show_glyph_safe_notice)
    };
    startup_profile::mark("setup_hints");

    // Best-effort: make sure the macOS menu bar session-count indicator is
    // running so it shows up automatically for every macOS user.
    commands::ensure_menubar_helper_running();

    if args.resume.is_none() {
        terminal::show_crash_resume_hint();
    }
    startup_profile::mark("crash_resume_hint");

    let cwd = std::env::current_dir()?;
    let in_jcode_repo = build::is_jcode_repo(&cwd);
    startup_profile::mark("is_jcode_repo");
    let already_in_selfdev = crate::cli::selfdev::client_selfdev_requested();

    // Record where this interactive launch happened so the system-wide launch
    // hotkeys can reopen jcode in the last project directory (Cmd+') and the
    // last jcode repo for self-dev (Cmd+Shift+'). Best-effort; ignored unless a
    // real TTY and not a fresh-spawn re-entry.
    if !args.fresh_spawn && std::io::IsTerminal::is_terminal(&std::io::stdin()) {
        let repo_dir = build::get_repo_dir();
        setup_hints::record_launch_dirs(&cwd, repo_dir.as_deref());
    }

    if in_jcode_repo && !already_in_selfdev && !args.no_selfdev {
        output::stderr_info("📍 Detected jcode repository - enabling self-dev mode");
        output::stderr_info("   Using shared server with self-dev session mode");
        output::stderr_info("   (use --no-selfdev to disable auto-detection)");
        output::stderr_blank_line();

        crate::env::set_var(selfdev::CLIENT_SELFDEV_ENV, "1");
        crate::cli::proctitle::set_initial_title(&args);
    }

    startup_profile::mark("client_mode_start");
    // The terminal background (OSC 11) query is a blocking round trip that used
    // to sit directly in front of TUI init. Start it here so it overlaps the
    // server check/spawn below. Safe only because nothing has entered raw mode
    // or started reading stdin yet, and it is skipped for exec handoffs where
    // the inherited terminal is already live.
    if std::env::var_os("JCODE_RESUMING").is_none() {
        crate::tui::theme_detect::prewarm_theme_mode();
    }
    let mut server_running = if args.fresh_spawn {
        true
    } else {
        server_is_running().await
    };
    startup_profile::mark("server_check");

    if !server_running {
        server_running = wait_for_existing_reload_server("client startup").await;
    }

    if !server_running && std::env::var("JCODE_RESUMING").is_ok() {
        server_running = wait_for_resuming_server(
            "client startup without reload marker",
            std::time::Duration::from_secs(5),
        )
        .await;
    }

    if server_running && explicit_provider_or_model {
        output::stderr_info(
            "Server already running; provider/model flags only apply when starting a new server.",
        );
        output::stderr_info(format!(
            "Current server settings control `/model`. Restart server to apply: --provider {}{}",
            args.provider.as_arg_value(),
            args.model
                .as_ref()
                .map(|m| format!(" --model {}", m))
                .unwrap_or_default()
        ));
    }

    if server_running && explicit_tool_options {
        output::stderr_info(
            "Server already running; tool flags only apply when starting a new server. Restart server or edit [tools] in config.toml to change the active toolset.",
        );
    }

    if !server_running {
        // No live server and no in-flight reload/resume. If a dead socket was
        // left behind by a crashed or upgraded daemon, reap it now so the spawn
        // below binds cleanly instead of wedging the client in a connect-retry
        // loop against a stale socket (issues #277/#291). This only removes a
        // socket that has no live listener AND whose daemon lock is free, so it
        // can never disturb a running server.
        if server::reap_stale_socket_if_dead(&server::socket_path()).await {
            output::stderr_info("Removed a stale jcode socket from a previous server.");
        }

        maybe_prompt_server_bootstrap_login(&args.provider).await?;
        spawn_server(
            &args.provider,
            args.model.as_deref(),
            args.provider_profile.as_deref(),
        )
        .await?;
    }

    startup_profile::mark("pre_tui_client");
    if std::env::var("JCODE_RESUMING").is_err() && server_running {
        output::stderr_info("Connecting to server...");
    }
    tui_launch::run_tui_client(
        args.resume,
        startup_hints,
        !server_running,
        args.fresh_spawn,
        args.remote_working_dir,
        args.onboarding_sim,
    )
    .await?;

    Ok(())
}

fn print_provider_test_coverage_report(report: &str, colorize: bool) {
    if colorize {
        print!(
            "{}",
            crate::live_tests::colorize_provider_test_coverage_output(report)
        );
    } else {
        print!("{}", report);
    }
}

pub(crate) async fn server_is_running() -> bool {
    server_is_running_at(&server::socket_path()).await
}

async fn wait_for_existing_reload_server(context: &str) -> bool {
    if let Some(state) = server::recent_reload_state(std::time::Duration::from_secs(30)) {
        match state.phase {
            server::ReloadPhase::Starting => {
                crate::logging::info(&format!(
                    "Reload state=starting during {}; waiting for existing server to return",
                    context
                ));
                return wait_for_reloading_server().await;
            }
            server::ReloadPhase::Failed => {
                crate::logging::warn(&format!(
                    "Reload state=failed during {} on {}: {}; recent_state={}",
                    context,
                    server::socket_path().display(),
                    state
                        .detail
                        .unwrap_or_else(|| "unknown reload failure".to_string()),
                    server::reload_state_summary(std::time::Duration::from_secs(60))
                ));
            }
            server::ReloadPhase::SocketReady => {}
        }
    }

    false
}

pub(crate) async fn wait_for_resuming_server(context: &str, timeout: std::time::Duration) -> bool {
    let socket_path = server::socket_path();
    let start = std::time::Instant::now();
    let mut announced = false;

    while start.elapsed() < timeout {
        if server_is_running_at(&socket_path).await {
            crate::logging::info(&format!(
                "Server became available during resume wait for {} after {}ms",
                context,
                start.elapsed().as_millis()
            ));
            return true;
        }

        if !announced {
            crate::logging::info(&format!(
                "Server not ready during {}; waiting up to {}ms for a resumed/reloading server before spawning a replacement",
                context,
                timeout.as_millis()
            ));
            announced = true;
        }

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }

    false
}

pub(crate) async fn wait_for_reloading_server() -> bool {
    match server::await_reload_handoff(&server::socket_path(), std::time::Duration::from_secs(30))
        .await
    {
        server::ReloadWaitStatus::Ready => true,
        server::ReloadWaitStatus::Failed(detail) => {
            crate::logging::warn(&format!(
                "Reload handoff failed while waiting for server on {}: {}; recent_state={}",
                server::socket_path().display(),
                detail.unwrap_or_else(|| "unknown reload failure".to_string()),
                server::reload_state_summary(std::time::Duration::from_secs(60))
            ));
            false
        }
        server::ReloadWaitStatus::Idle => false,
        server::ReloadWaitStatus::Waiting { .. } => false,
    }
}

async fn server_is_running_at(path: &std::path::Path) -> bool {
    // Check liveness before performing a protocol handshake. On Windows the
    // named pipe may be busy while another client is connecting; that already
    // proves a daemon exists, while a handshake connect can otherwise wait in
    // the transport's ERROR_PIPE_BUSY retry loop and block server startup.
    server::has_live_listener(path).await || server::is_server_ready(path).await
}

#[cfg(unix)]
fn spawn_lock_path(socket_path: &std::path::Path) -> std::path::PathBuf {
    std::path::PathBuf::from(format!("{}.spawning", socket_path.display()))
}

#[cfg(unix)]
struct SpawnLockGuard {
    _file: std::fs::File,
    path: std::path::PathBuf,
}

#[cfg(unix)]
impl Drop for SpawnLockGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

#[cfg(unix)]
fn try_acquire_spawn_lock(path: &std::path::Path) -> Result<Option<SpawnLockGuard>> {
    use std::fs::OpenOptions;
    use std::os::fd::AsRawFd;

    let file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(path)?;
    let fd = file.as_raw_fd();
    let ret = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
    if ret == 0 {
        Ok(Some(SpawnLockGuard {
            _file: file,
            path: path.to_path_buf(),
        }))
    } else {
        Ok(None)
    }
}

#[cfg(unix)]
async fn acquire_spawn_lock_or_wait(
    socket_path: &std::path::Path,
) -> Result<Option<SpawnLockGuard>> {
    let lock_path = spawn_lock_path(socket_path);
    let wait_start = std::time::Instant::now();
    let wait_timeout = std::time::Duration::from_secs(10);
    let mut announced_wait = false;

    loop {
        if let Some(lock) = try_acquire_spawn_lock(&lock_path)? {
            return Ok(Some(lock));
        }

        if server_is_running_at(socket_path).await {
            return Ok(None);
        }

        if !announced_wait {
            output::stderr_info("Another client is starting the server, waiting...");
            announced_wait = true;
        }

        if wait_start.elapsed() >= wait_timeout {
            anyhow::bail!(
                "Timed out waiting for another client to start server at {}",
                socket_path.display()
            );
        }

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
}

pub(crate) async fn maybe_prompt_server_bootstrap_login(
    provider_choice: &ProviderChoice,
) -> Result<()> {
    startup_profile::mark("cred_check_start");

    // Normal interactive launches perform onboarding inside the TUI, and an
    // explicit provider choice never needs auto-detection here. Avoid probing
    // every credential backend unless the caller explicitly opted into the
    // legacy headless CLI bootstrap flow. On Windows those reads may trigger
    // expensive security-product inspection even when credentials are already
    // configured, delaying every cold launch before the server is spawned.
    let cli_bootstrap_requested = std::env::var_os("JCODE_CLI_BOOTSTRAP_LOGIN").is_some();
    if !should_detect_cli_bootstrap_credentials(provider_choice, cli_bootstrap_requested) {
        startup_profile::mark("cred_check_done");
        return Ok(());
    }

    let cred_state = detect_bootstrap_credentials().await;
    startup_profile::mark("cred_check_done");

    // Onboarding now happens entirely inside the TUI. We deliberately do *not*
    // run the blocking CLI "Approve sources" import prompt or the
    // "Choose a provider" selection menu here: a brand-new user launches
    // straight into the TUI, which detects the missing credentials and walks
    // them through login / external-auth import / model selection in the guided
    // first-run flow. The server is happy to spawn unauthenticated and the TUI
    // drives `/login` from there.
    //
    // The only thing left to honor at the CLI layer is an explicit headless
    // bootstrap (e.g. CI / non-interactive provisioning), which opts in via the
    // `JCODE_CLI_BOOTSTRAP_LOGIN` env var.
    if cred_state.has_any {
        return Ok(());
    }

    if auth::AuthStatus::has_any_untrusted_external_auth() {
        let _ = provider_init::maybe_run_external_auth_auto_import_flow().await?;
        if detect_bootstrap_credentials().await.has_any {
            return Ok(());
        }
    }

    let provider = provider_init::prompt_login_provider_selection(
        &provider_catalog::server_bootstrap_login_providers(),
        "No credentials found. Let's log in!\n\nChoose a provider:",
    )?;
    login::run_login_provider(provider, None, login::LoginOptions::default()).await?;
    provider_init::apply_login_provider_profile_env(provider);
    output::stderr_blank_line();

    Ok(())
}

fn should_detect_cli_bootstrap_credentials(
    provider_choice: &ProviderChoice,
    cli_bootstrap_requested: bool,
) -> bool {
    cli_bootstrap_requested && *provider_choice == ProviderChoice::Auto
}

struct BootstrapCredentialState {
    has_any: bool,
}

async fn detect_bootstrap_credentials() -> BootstrapCredentialState {
    let (has_claude, has_openai) = tokio::join!(
        tokio::task::spawn_blocking(|| auth::claude::load_credentials().is_ok()),
        tokio::task::spawn_blocking(|| auth::codex::load_credentials().is_ok()),
    );
    let has_claude = has_claude.unwrap_or(false);
    let has_openai = has_openai.unwrap_or(false);
    let has_openrouter = provider::openrouter::has_credentials();
    let has_copilot = auth::copilot::has_copilot_credentials();
    let has_api_key = std::env::var("ANTHROPIC_API_KEY").is_ok();

    BootstrapCredentialState {
        has_any: has_claude || has_openai || has_openrouter || has_copilot || has_api_key,
    }
}

pub(crate) async fn spawn_server(
    provider_choice: &ProviderChoice,
    model: Option<&str>,
    provider_profile: Option<&str>,
) -> Result<()> {
    let socket_path = server::socket_path();
    if server_is_running_at(&socket_path).await {
        startup_profile::mark("server_ready");
        return Ok(());
    }

    if wait_for_existing_reload_server("server spawn").await {
        startup_profile::mark("server_ready");
        return Ok(());
    }

    #[cfg(unix)]
    let _spawn_lock = acquire_spawn_lock_or_wait(&socket_path).await?;

    if server_is_running_at(&socket_path).await {
        startup_profile::mark("server_ready");
        return Ok(());
    }

    if wait_for_existing_reload_server("server spawn after lock").await {
        startup_profile::mark("server_ready");
        return Ok(());
    }

    startup_profile::mark("server_spawn_start");
    output::stderr_info("Starting server...");
    let client_requested_selfdev = selfdev::client_selfdev_requested();
    let exe = build::shared_server_update_candidate(client_requested_selfdev)
        .map(|(path, _)| path)
        .or_else(|| std::env::current_exe().ok())
        .ok_or_else(|| anyhow::anyhow!("Could not determine executable path for server spawn"))?;
    let mut cmd = ProcessCommand::new(&exe);
    cmd.env_remove(selfdev::CLIENT_SELFDEV_ENV);
    if client_requested_selfdev {
        cmd.env("JCODE_DEBUG_CONTROL", "1");
    }
    cmd.arg("--provider").arg(provider_choice.as_arg_value());
    // The interactive TUI owns first-run onboarding/login. Let the spawned
    // server boot with a deferred (credential-less) provider when nothing is
    // configured yet, instead of bailing; the TUI activates a provider via the
    // in-TUI `/login` flow. See init_provider_with_options.
    cmd.env("JCODE_DEFERRED_AUTH_BOOTSTRAP", "1");
    if let Some(provider_profile) = provider_profile {
        cmd.arg("--provider-profile").arg(provider_profile);
    }
    if let Some(model) = model {
        cmd.arg("--model").arg(model);
    }
    cmd.arg("serve")
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    #[cfg(unix)]
    {
        let _child = server::spawn_server_notify(&mut cmd).await?;
        startup_profile::mark("server_ready");
    }
    #[cfg(not(unix))]
    {
        use std::io::Read;

        let mut child = cmd.spawn()?;
        let start = std::time::Instant::now();
        // Windows server bootstrap can legitimately take tens of seconds on
        // slow hosts (auth preflights + provider init were observed at 15-60s
        // on a Windows Server VPS, issue #503). The child's liveness is
        // checked every poll, so a generous budget only delays the error for
        // a genuinely hung server, while a crashed server still fails fast
        // with its stderr.
        let timeout = std::time::Duration::from_secs(120);
        while start.elapsed() < timeout {
            if server::has_live_listener(&socket_path).await {
                startup_profile::mark("server_ready");
                return Ok(());
            }

            if let Some(status) = child.try_wait()? {
                let mut stderr = String::new();
                if let Some(mut pipe) = child.stderr.take() {
                    let _ = pipe.read_to_string(&mut stderr);
                }
                let detail = stderr.trim();
                if detail.is_empty() {
                    anyhow::bail!("Server exited before becoming ready (status: {})", status);
                }
                anyhow::bail!(
                    "Server exited before becoming ready (status: {}). {}",
                    status,
                    detail
                );
            }

            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }

        anyhow::bail!(
            "Timed out waiting for server to become ready at {} after {}ms",
            server::socket_path().display(),
            timeout.as_millis()
        );
    }

    #[cfg(unix)]
    Ok(())
}

async fn run_server_keepalive(
    provider_choice: &ProviderChoice,
    model: Option<&str>,
    provider_profile: Option<&str>,
) -> Result<()> {
    let mut owner_closed = tokio::task::spawn_blocking(|| {
        let mut stdin = std::io::stdin();
        let mut buffer = [0u8; 256];
        loop {
            match std::io::Read::read(&mut stdin, &mut buffer) {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
        }
    });
    let mut client: Option<server::Client> = None;
    let mut first_attempt = true;

    loop {
        let delay = if first_attempt {
            first_attempt = false;
            std::time::Duration::ZERO
        } else if client.is_some() {
            std::time::Duration::from_secs(30)
        } else {
            std::time::Duration::from_secs(1)
        };
        tokio::select! {
            _ = &mut owner_closed => return Ok(()),
            _ = tokio::time::sleep(delay) => {
                if client.is_some() {
                    // A Ping is a one-shot control request, so sending it over
                    // the held connection would make the server close that
                    // connection after replying. Probe through a short-lived
                    // client instead and leave the counted keepalive connected.
                    let healthy = tokio::time::timeout(
                        std::time::Duration::from_secs(5),
                        async {
                            let mut probe = server::Client::connect().await?;
                            probe.ping().await
                        },
                    )
                    .await
                    .is_ok_and(|result| result.unwrap_or(false));
                    if healthy {
                        continue;
                    }
                    client = None;
                }
                if spawn_server(provider_choice, model, provider_profile).await.is_ok()
                    && let Ok(connected) = server::Client::connect().await
                {
                    client = Some(connected);
                }
            }
        }
    }
}

#[cfg(test)]
#[path = "dispatch_tests.rs"]
mod dispatch_tests;
