use super::*;
use anyhow::{Result, anyhow};

#[tokio::test]
async fn handle_clear_session_replaces_runtime_handles_and_updates_shutdown_registration()
-> Result<()> {
    let _guard = crate::storage::lock_test_env();

    let old_session_id = "session_before_clear";
    let provider: Arc<dyn Provider> = Arc::new(MockProvider);
    let registry = Registry::new(provider.clone()).await;
    let agent = Arc::new(Mutex::new(build_test_agent_with_id(
        provider.clone(),
        registry.clone(),
        old_session_id,
        Vec::new(),
    )));

    let old_queue = {
        let guard = agent.lock().await;
        guard.soft_interrupt_queue()
    };
    let old_background_signal = {
        let guard = agent.lock().await;
        guard.background_tool_signal()
    };
    let old_cancel_signal = {
        let guard = agent.lock().await;
        guard.graceful_shutdown_signal()
    };

    let sessions = Arc::new(RwLock::new(HashMap::from([(
        old_session_id.to_string(),
        Arc::clone(&agent),
    )])));
    let shutdown_signals = Arc::new(RwLock::new(HashMap::from([(
        old_session_id.to_string(),
        old_cancel_signal.clone(),
    )])));
    let soft_interrupt_queues: SessionInterruptQueues = Arc::new(RwLock::new(HashMap::from([(
        old_session_id.to_string(),
        old_queue.clone(),
    )])));
    let now = Instant::now();
    let client_connections = Arc::new(RwLock::new(HashMap::from([(
        "conn_clear".to_string(),
        ClientConnectionInfo {
            client_id: "conn_clear".to_string(),
            session_id: old_session_id.to_string(),
            client_instance_id: None,
            debug_client_id: Some("debug_clear".to_string()),
            connected_at: now,
            last_seen: now,
            is_processing: false,
            current_tool_name: None,
            terminal_env: Vec::new(),
            disconnect_tx: mpsc::unbounded_channel().0,
        },
    )])));
    let swarm_members = Arc::new(RwLock::new(HashMap::from([(
        old_session_id.to_string(),
        test_swarm_member(old_session_id, "ready"),
    )])));
    let swarms_by_id = Arc::new(RwLock::new(HashMap::from([(
        "swarm-test".to_string(),
        HashSet::from([old_session_id.to_string()]),
    )])));
    let file_touch = FileTouchService::new();
    let channel_subscriptions = Arc::new(RwLock::new(HashMap::<
        String,
        HashMap<String, HashSet<String>>,
    >::new()));
    let channel_subscriptions_by_session = Arc::new(RwLock::new(HashMap::<
        String,
        HashMap<String, HashSet<String>>,
    >::new()));
    let swarm_plans = Arc::new(RwLock::new(HashMap::from([(
        "swarm-test".to_string(),
        VersionedPlan {
            items: Vec::new(),
            version: 1,
            participants: HashSet::from([old_session_id.to_string()]),
            task_progress: HashMap::new(),
            mode: "deep".to_string(),
            node_meta: HashMap::new(),
        },
    )])));
    let event_history = Arc::new(RwLock::new(VecDeque::<SwarmEvent>::new()));
    let event_counter = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let (swarm_event_tx, _swarm_event_rx) = broadcast::channel::<SwarmEvent>(8);
    let (client_event_tx, mut client_event_rx) = mpsc::unbounded_channel::<ServerEvent>();

    let mut client_session_id = old_session_id.to_string();
    handle_clear_session(
        7,
        false,
        &mut client_session_id,
        "conn_clear",
        &agent,
        &provider,
        &registry,
        &sessions,
        &shutdown_signals,
        &soft_interrupt_queues,
        &client_connections,
        &swarm_members,
        &swarms_by_id,
        &file_touch,
        &channel_subscriptions,
        &channel_subscriptions_by_session,
        &swarm_plans,
        &event_history,
        &event_counter,
        &swarm_event_tx,
        &client_event_tx,
    )
    .await;

    assert_ne!(client_session_id, old_session_id);
    let members = swarm_members.read().await;
    assert!(members.get(old_session_id).is_none());
    let replacement_member = members
        .get(&client_session_id)
        .expect("replacement session should remain registered for swarm tools");
    assert!(replacement_member.swarm_enabled);
    assert_eq!(replacement_member.status, "ready");
    assert_ne!(replacement_member.swarm_id.as_deref(), Some("swarm-test"));
    let replacement_swarm_id = replacement_member
        .swarm_id
        .clone()
        .expect("replacement session should get a fresh swarm identity");
    drop(members);
    assert!(swarms_by_id.read().await.get("swarm-test").is_none());
    assert!(
        swarms_by_id
            .read()
            .await
            .get(&replacement_swarm_id)
            .is_some_and(|sessions| sessions.contains(&client_session_id))
    );
    let plans = swarm_plans.read().await;
    assert!(!plans["swarm-test"].participants.contains(old_session_id));
    assert!(
        !plans["swarm-test"]
            .participants
            .contains(&client_session_id)
    );
    drop(plans);

    old_queue
        .lock()
        .map_err(|_| anyhow!("old queue lock"))?
        .push(jcode_agent_runtime::SoftInterruptMessage {
            content: "stale queued message".to_string(),
            images: Vec::new(),
            urgent: false,
            source: jcode_agent_runtime::SoftInterruptSource::User,
        });
    old_background_signal.fire();
    old_cancel_signal.fire();

    let (new_queue, new_background_signal, new_cancel_signal) = {
        let guard = agent.lock().await;
        (
            guard.soft_interrupt_queue(),
            guard.background_tool_signal(),
            guard.graceful_shutdown_signal(),
        )
    };

    assert!(!Arc::ptr_eq(&old_queue, &new_queue));
    assert!(!new_background_signal.is_set());
    assert!(!new_cancel_signal.is_set());
    assert!(!agent.lock().await.has_soft_interrupts());

    let queue_map = soft_interrupt_queues.read().await;
    assert!(!queue_map.contains_key(old_session_id));
    assert!(queue_map.contains_key(&client_session_id));
    drop(queue_map);

    let signals = shutdown_signals.read().await;
    assert!(!signals.contains_key(old_session_id));
    let registered_signal = signals
        .get(&client_session_id)
        .ok_or_else(|| anyhow!("new session should have shutdown signal"))?
        .clone();
    drop(signals);
    registered_signal.fire();
    assert!(new_cancel_signal.is_set());

    let first = client_event_rx
        .recv()
        .await
        .ok_or_else(|| anyhow!("session id event"))?;
    assert!(matches!(first, ServerEvent::SessionId { .. }));
    let second = client_event_rx
        .recv()
        .await
        .ok_or_else(|| anyhow!("done event"))?;
    assert!(matches!(second, ServerEvent::Done { id: 7 }));
    Ok(())
}

/// `/clear` must fire the `session_end` hook for the session it discards.
///
/// External session-end consumers (memory capture, vault refresh) treat
/// `session_end` as "this transcript is over, summarize it". `/clear` ends a
/// transcript just as definitively as closing the app, so a `/clear` that
/// stays silent silently drops that session's summary, and the loss is
/// invisible: nothing errors, the memory simply never appears.
#[tokio::test]
async fn handle_clear_session_fires_the_session_end_hook() -> Result<()> {
    let _guard = crate::storage::lock_test_env();

    let dir = std::env::temp_dir().join(format!("jcode-clear-hook-{}", std::process::id()));
    std::fs::create_dir_all(&dir)?;
    let log = dir.join("fired.log");
    let script = dir.join("hook.sh");
    let _ = std::fs::remove_file(&log);
    std::fs::write(
        &script,
        format!(
            "#!/bin/sh\necho \"$JCODE_HOOK_SOURCE $JCODE_HOOK_SESSION_ID\" >> {}\n",
            log.display()
        ),
    )?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755))?;
    }

    // SAFETY: the storage test lock serializes env mutation across these tests.
    unsafe {
        std::env::set_var("JCODE_HOOK_SESSION_END", script.to_string_lossy().as_ref());
    }
    crate::config::invalidate_config_cache();

    let old_session_id = "session_before_clear_hook";
    let provider: Arc<dyn Provider> = Arc::new(MockProvider);
    let registry = Registry::new(provider.clone()).await;
    let agent = Arc::new(Mutex::new(build_test_agent_with_id(
        provider.clone(),
        registry.clone(),
        old_session_id,
        Vec::new(),
    )));

    let sessions = Arc::new(RwLock::new(HashMap::from([(
        old_session_id.to_string(),
        Arc::clone(&agent),
    )])));
    let shutdown_signals = Arc::new(RwLock::new(HashMap::new()));
    let soft_interrupt_queues: SessionInterruptQueues = Arc::new(RwLock::new(HashMap::new()));
    let client_connections = Arc::new(RwLock::new(HashMap::new()));
    let swarm_members = Arc::new(RwLock::new(HashMap::new()));
    let swarms_by_id = Arc::new(RwLock::new(HashMap::new()));
    let file_touch = FileTouchService::new();
    let channel_subscriptions = Arc::new(RwLock::new(HashMap::new()));
    let channel_subscriptions_by_session = Arc::new(RwLock::new(HashMap::new()));
    let swarm_plans = Arc::new(RwLock::new(HashMap::new()));
    let event_history = Arc::new(RwLock::new(VecDeque::<SwarmEvent>::new()));
    let event_counter = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let (swarm_event_tx, _swarm_event_rx) = broadcast::channel::<SwarmEvent>(8);
    let (client_event_tx, _client_event_rx) = mpsc::unbounded_channel::<ServerEvent>();

    // Drive the real `/clear` request handler, not `mark_closed` directly:
    // the point of the test is that this path reaches the hook.
    let mut client_session_id = old_session_id.to_string();
    handle_clear_session(
        11,
        false,
        &mut client_session_id,
        "conn_clear_hook",
        &agent,
        &provider,
        &registry,
        &sessions,
        &shutdown_signals,
        &soft_interrupt_queues,
        &client_connections,
        &swarm_members,
        &swarms_by_id,
        &file_touch,
        &channel_subscriptions,
        &channel_subscriptions_by_session,
        &swarm_plans,
        &event_history,
        &event_counter,
        &swarm_event_tx,
        &client_event_tx,
    )
    .await;

    // The hook is spawned detached, so poll briefly rather than assuming it
    // has already run by the time mark_closed returns.
    let mut fired = String::new();
    for _ in 0..100 {
        if let Ok(text) = std::fs::read_to_string(&log)
            && !text.trim().is_empty()
        {
            fired = text;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }

    // SAFETY: see above.
    unsafe {
        std::env::remove_var("JCODE_HOOK_SESSION_END");
    }
    crate::config::invalidate_config_cache();
    let _ = std::fs::remove_dir_all(&dir);

    assert!(
        fired.contains(old_session_id),
        "session_end hook must fire for the discarded session, got {fired:?}"
    );
    Ok(())
}
