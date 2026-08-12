//! Session-lifetime tracker for `[graft]` MCP tool token-savings footers.
//!
//! The `graft` MCP server (https://github.com/parcadei/graft, `@nanonets/graft`)
//! appends a footer to its tool output of the form:
//!
//! ```text
//! [graft] tokens saved ≈ 12,345 (78%); this pack ≈ 1,234 tok vs reading the 3 file(s) whole ≈ 13,579 tok
//! ```
//!
//! Claude Code has a dedicated hook/statusline that parses this and shows a
//! running `~N tok saved` total. jcode has no equivalent adapter shipped by
//! graft, so this module fills that gap generically: any MCP tool output
//! (from any server) is scanned for the footer, and matches are summed into a
//! process-lifetime session total that the `GraftSavings` info widget reads.
//!
//! This is intentionally a lightweight global accumulator (not threaded
//! through `App`/session state) so the hook point in `mcp/tool.rs` stays a
//! one-line call with no plumbing through the agent/tool trait boundary.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

/// Total tokens saved (summed across every `[graft] tokens saved ≈ N` footer
/// seen this process lifetime).
static TOTAL_SAVED_TOKENS: AtomicU64 = AtomicU64::new(0);
/// Number of tool calls whose output contained at least one footer.
static ATTRIBUTED_CALLS: AtomicU64 = AtomicU64::new(0);

/// Most recent attributions, newest last, capped at `MAX_RECENT`.
static RECENT: Mutex<Vec<GraftSavingsSample>> = Mutex::new(Vec::new());
const MAX_RECENT: usize = 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GraftSavingsSample {
    pub tokens: u64,
    pub percent: Option<u8>,
}

/// Point-in-time snapshot of the session's accumulated graft savings.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct GraftSavingsSnapshot {
    pub total_tokens: u64,
    pub attributed_calls: u64,
}

impl GraftSavingsSnapshot {
    pub fn is_empty(&self) -> bool {
        self.attributed_calls == 0
    }
}

/// Scan `output` for `[graft] tokens saved ≈ N` footers and add every match to
/// the session total. Safe to call on output from any tool; non-graft output
/// simply won't match. Returns the number of tokens recorded from this call
/// (0 if no footer was found).
pub fn record_from_output(output: &str) -> u64 {
    let mut recorded = 0u64;
    let mut hit = false;

    for (tokens, percent) in parse_footers(output) {
        recorded = recorded.saturating_add(tokens);
        hit = true;
        if let Ok(mut recent) = RECENT.lock() {
            recent.push(GraftSavingsSample { tokens, percent });
            if recent.len() > MAX_RECENT {
                let excess = recent.len() - MAX_RECENT;
                recent.drain(0..excess);
            }
        }
    }

    if hit {
        TOTAL_SAVED_TOKENS.fetch_add(recorded, Ordering::Relaxed);
        ATTRIBUTED_CALLS.fetch_add(1, Ordering::Relaxed);
    }

    recorded
}

/// Parse every `[graft] tokens saved ≈ N` (optionally `(P%)`) footer in `text`.
/// Numbers may contain thousands separators (`,`).
fn parse_footers(text: &str) -> Vec<(u64, Option<u8>)> {
    const MARKER: &str = "[graft] tokens saved";
    let mut out = Vec::new();
    let mut rest = text;

    while let Some(idx) = rest.find(MARKER) {
        let after = &rest[idx + MARKER.len()..];
        // Skip past "≈ " (or "~"/"=" fallbacks) to the digits.
        let digits_start = after
            .find(|c: char| c.is_ascii_digit())
            .filter(|&pos| pos < 8); // only accept it right after the marker
        if let Some(start) = digits_start {
            let digits_str = &after[start..];
            let end = digits_str
                .find(|c: char| !(c.is_ascii_digit() || c == ','))
                .unwrap_or(digits_str.len());
            let raw = &digits_str[..end];
            let cleaned: String = raw.chars().filter(|c| *c != ',').collect();
            if let Ok(tokens) = cleaned.parse::<u64>() {
                let tail = &digits_str[end..];
                let percent = tail
                    .find('(')
                    .and_then(|p| {
                        let after_paren = &tail[p + 1..];
                        let pct_end = after_paren.find('%')?;
                        after_paren[..pct_end].trim().parse::<u8>().ok()
                    });
                out.push((tokens, percent));
            }
        }
        rest = &rest[idx + MARKER.len()..];
    }

    out
}

/// Current session snapshot.
pub fn snapshot() -> GraftSavingsSnapshot {
    GraftSavingsSnapshot {
        total_tokens: TOTAL_SAVED_TOKENS.load(Ordering::Relaxed),
        attributed_calls: ATTRIBUTED_CALLS.load(Ordering::Relaxed),
    }
}

/// Most recent per-call samples, oldest first.
pub fn recent_samples() -> Vec<GraftSavingsSample> {
    RECENT.lock().map(|r| r.clone()).unwrap_or_default()
}

/// Reset all tracked state. Exposed for tests only.
#[cfg(any(test, feature = "test-support"))]
pub fn reset_for_tests() {
    TOTAL_SAVED_TOKENS.store(0, Ordering::Relaxed);
    ATTRIBUTED_CALLS.store(0, Ordering::Relaxed);
    if let Ok(mut recent) = RECENT.lock() {
        recent.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    // The tracker uses process-global state, so tests must not run
    // concurrently against each other (cargo test runs tests in parallel
    // threads by default within one process).
    static TEST_LOCK: StdMutex<()> = StdMutex::new(());

    fn reset() -> std::sync::MutexGuard<'static, ()> {
        let guard = TEST_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        reset_for_tests();
        guard
    }

    #[test]
    fn parses_single_footer() {
        let _guard = reset();
        let out = "some result\n[graft] tokens saved ≈ 1,234 (78%); this pack ≈ 100 tok vs reading the 3 file(s) whole ≈ 1,334 tok";
        let recorded = record_from_output(out);
        assert_eq!(recorded, 1234);
        let snap = snapshot();
        assert_eq!(snap.total_tokens, 1234);
        assert_eq!(snap.attributed_calls, 1);
        assert!(!snap.is_empty());
    }

    #[test]
    fn accumulates_across_calls() {
        let _guard = reset();
        record_from_output("[graft] tokens saved ≈ 500 (10%)");
        record_from_output("[graft] tokens saved ≈ 750 (20%)");
        let snap = snapshot();
        assert_eq!(snap.total_tokens, 1250);
        assert_eq!(snap.attributed_calls, 2);
    }

    #[test]
    fn sums_multiple_footers_in_one_output() {
        let _guard = reset();
        let out = "\
[graft] tokens saved ≈ 100 (5%)
---
[graft] tokens saved ≈ 200 (10%)";
        let recorded = record_from_output(out);
        assert_eq!(recorded, 300);
        assert_eq!(snapshot().attributed_calls, 1);
    }

    #[test]
    fn ignores_output_without_footer() {
        let _guard = reset();
        let recorded = record_from_output("plain tool output, nothing graft-related");
        assert_eq!(recorded, 0);
        assert!(snapshot().is_empty());
    }

    #[test]
    fn handles_percent_missing() {
        let _guard = reset();
        let recorded = record_from_output("[graft] tokens saved ≈ 42");
        assert_eq!(recorded, 42);
    }
}
