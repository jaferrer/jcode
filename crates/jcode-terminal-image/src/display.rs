//! Terminal image display support
//!
//! Supports Kitty graphics protocol (Kitty, Ghostty, Handterm), iTerm2 inline images,
//! and Sixel graphics (xterm, foot, mlterm, WezTerm).
//! Falls back to a simple placeholder if no image protocol is available.

use base64::{Engine, engine::general_purpose::STANDARD as BASE64};
use image::ImageEncoder;
use std::io::{self, BufRead, BufReader, IsTerminal, Write};
use std::path::Path;
use std::process::Command;
use std::sync::{
    LazyLock,
    atomic::{AtomicU64, Ordering},
};

#[cfg(unix)]
use std::os::unix::net::UnixStream;

/// Cache whether ImageMagick is available for Sixel conversion
static NEXT_HERDR_REQUEST_ID: AtomicU64 = AtomicU64::new(1);

static HAS_IMAGEMAGICK: LazyLock<bool> = LazyLock::new(|| {
    Command::new("convert")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
});

/// Terminal image protocol support
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ImageProtocol {
    /// Herdr pane graphics API (the multiplexer owns Kitty placement).
    Herdr,
    /// Kitty graphics protocol (most feature-rich)
    Kitty,
    /// iTerm2 inline images
    ITerm2,
    /// Sixel graphics (xterm, foot, mlterm, WezTerm)
    Sixel,
    /// No image support
    None,
}

/// Placement for an image in a Herdr pane viewport.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HerdrImagePlacement {
    pub viewport_col: i32,
    pub viewport_row: i32,
    pub grid_cols: u32,
    pub grid_rows: u32,
}

impl ImageProtocol {
    /// Detect the best available image protocol for the current terminal
    pub fn detect() -> Self {
        // Herdr consumes APC graphics emitted by programs inside its panes, so
        // use its pane API when the user opts in and the client exports the
        // socket and pane ID. Without the opt-in the in-buffer halfblocks path
        // is used, which paints in any terminal.
        if herdr_image_env(
            std::env::var("JCODE_HERDR_IMAGES").ok().as_deref(),
            std::env::var("HERDR_ENV").ok().as_deref(),
            std::env::var("HERDR_SOCKET_PATH").ok().as_deref(),
            std::env::var("HERDR_PANE_ID").ok().as_deref(),
        ) {
            return Self::Herdr;
        }

        // Check for Kitty first (most capable)
        if std::env::var("KITTY_WINDOW_ID").is_ok() {
            return Self::Kitty;
        }

        // Check TERM for Kitty-compatible terminals.
        if let Ok(term) = std::env::var("TERM")
            && is_kitty_terminal_name(&term)
        {
            return Self::Kitty;
        }

        // Check TERM_PROGRAM for Kitty-compatible terminals.
        if let Ok(term_program) = std::env::var("TERM_PROGRAM") {
            if is_kitty_terminal_name(&term_program) {
                return Self::Kitty;
            }
            if term_program == "iTerm.app" {
                return iterm2_protocol();
            }
            // WezTerm supports Sixel
            if term_program == "WezTerm" {
                return Self::Sixel;
            }
        }

        // Check LC_TERMINAL for iTerm2
        if let Ok(lc_terminal) = std::env::var("LC_TERMINAL")
            && lc_terminal == "iTerm2"
        {
            return iterm2_protocol();
        }

        // Check for Sixel-capable terminals
        if Self::detect_sixel() {
            return Self::Sixel;
        }

        Self::None
    }

    /// Detect if terminal supports Sixel graphics
    fn detect_sixel() -> bool {
        // Only enable Sixel if we have ImageMagick to do the conversion
        if !*HAS_IMAGEMAGICK {
            return false;
        }

        if let Ok(term) = std::env::var("TERM") {
            let term_lower = term.to_lowercase();
            // Known Sixel-capable terminals
            if term_lower.contains("xterm")
                || term_lower.contains("foot")
                || term_lower.contains("mlterm")
                || term_lower.contains("yaft")
                || term_lower.contains("mintty")
                || term_lower.contains("contour")
            {
                return true;
            }
        }

        // Check TERM_PROGRAM for other Sixel terminals
        if let Ok(prog) = std::env::var("TERM_PROGRAM")
            && (prog == "mintty" || prog == "contour")
        {
            return true;
        }

        false
    }

    /// Check if image display is supported
    pub fn is_supported(&self) -> bool {
        *self != Self::None
    }
}

/// Terminal multiplexer escape wrapping, mirroring ratatui-image's
/// `Parser::escape_tmux`. tmux swallows unknown OSC/APC sequences unless they
/// are wrapped in its passthrough form with doubled escapes, which silently
/// drops image payloads (or leaks them as garbage text) inside tmux.
fn escape_tmux(is_tmux: bool) -> (&'static str, &'static str, &'static str) {
    if is_tmux {
        ("\x1bPtmux;", "\x1b\x1b", "\x1b\\")
    } else {
        ("", "\x1b", "")
    }
}

fn in_tmux() -> bool {
    std::env::var("TMUX").is_ok_and(|v| !v.trim().is_empty())
        || std::env::var("TERM").is_ok_and(|t| t.starts_with("tmux"))
}

/// iTerm2's inline-image protocol corrupts jcode's TUI output in real iTerm2,
/// so image display is disabled there unless the user opts back in with
/// `JCODE_ITERM2_IMAGES=1`.
fn iterm2_images_enabled() -> bool {
    matches!(
        std::env::var("JCODE_ITERM2_IMAGES")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn iterm2_protocol() -> ImageProtocol {
    if iterm2_images_enabled() {
        ImageProtocol::ITerm2
    } else {
        ImageProtocol::None
    }
}

fn is_kitty_terminal_name(value: &str) -> bool {
    let value = value.to_ascii_lowercase();
    value.contains("kitty") || value.contains("ghostty") || value.contains("handterm")
}

/// Whether inline images may be routed through Herdr's pane graphics API.
///
/// The API acknowledges every `pane.graphics.set` with `ok` even when the
/// attached client never paints the pixels, so a failed placement is
/// indistinguishable from a successful one on our side. Meanwhile the TUI skips
/// its own in-buffer draw for those regions, which leaves the reserved rows
/// blank and the image reads as a black rectangle.
///
/// Because the in-buffer halfblocks path renders correctly in any terminal,
/// Herdr routing is opt-in: set `JCODE_HERDR_IMAGES=1` to use it.
fn herdr_images_opt_in(raw: Option<&str>) -> bool {
    matches!(
        raw.map(|value| value.trim().to_ascii_lowercase()).as_deref(),
        Some("1" | "true" | "yes" | "on")
    )
}

fn herdr_image_env(
    opt_in: Option<&str>,
    herdr_env: Option<&str>,
    socket: Option<&str>,
    pane: Option<&str>,
) -> bool {
    herdr_images_opt_in(opt_in)
        && matches!(herdr_env, Some("1" | "true" | "yes" | "on"))
        && socket.is_some_and(|value| !value.is_empty())
        && pane.is_some_and(|value| !value.is_empty())
}

/// Display parameters for terminal images
#[derive(Debug, Clone)]
pub struct ImageDisplayParams {
    /// Maximum width in terminal columns
    pub max_cols: u16,
    /// Maximum height in terminal rows
    pub max_rows: u16,
}

impl Default for ImageDisplayParams {
    fn default() -> Self {
        Self {
            max_cols: 80,
            max_rows: 24,
        }
    }
}

impl ImageDisplayParams {
    /// Create display params based on terminal size
    pub fn from_terminal() -> Self {
        let (cols, rows) = crossterm::terminal::size().unwrap_or((120, 40));

        // Use about 2/3 of terminal width, capped at 100 columns
        // Use about 1/2 of terminal height, capped at 30 rows
        Self {
            max_cols: (cols * 2 / 3).clamp(40, 100),
            max_rows: (rows / 2).clamp(10, 30),
        }
    }
}

/// Display an image in the terminal
///
/// Returns Ok(true) if the image was displayed, Ok(false) if not supported,
/// or an error if something went wrong.
pub fn display_image(path: &Path, params: &ImageDisplayParams) -> io::Result<bool> {
    let protocol = ImageProtocol::detect();

    // Protocol environment variables such as TERM are often inherited by
    // headless commands whose stdout is redirected to an NDJSON file. Trying
    // to render in that case is both incorrect and can deadlock when the
    // caller already holds stdout's lock for the duration of the command.
    if !can_display_to_stdout(protocol, io::stdout().is_terminal()) {
        return Ok(false);
    }

    // Read the image file
    let data = std::fs::read(path)?;

    // Get image dimensions to calculate aspect ratio
    let (img_width, img_height) = get_image_dimensions(&data).unwrap_or((0, 0));

    match protocol {
        ImageProtocol::Herdr => display_herdr(&data, params, img_width, img_height),
        ImageProtocol::Kitty => display_kitty(&data, params, img_width, img_height),
        ImageProtocol::ITerm2 => display_iterm2(&data, path, params, img_width, img_height),
        ImageProtocol::Sixel => display_sixel(path, params, img_width, img_height),
        ImageProtocol::None => Ok(false),
    }
}

/// Display through Herdr's pane API instead of emitting APC bytes into the
/// pane. Herdr consumes program-emitted Kitty APC and currently drops the
/// placement, while this API path is the supported integration boundary.
#[cfg(unix)]
fn display_herdr(
    data: &[u8],
    _params: &ImageDisplayParams,
    img_width: u32,
    img_height: u32,
) -> io::Result<bool> {
    let socket = match std::env::var("HERDR_SOCKET_PATH") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(false),
    };
    let pane_id = match std::env::var("HERDR_PANE_ID") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(false),
    };
    send_herdr_graphics(&socket, &pane_id, data, img_width, img_height, None)
}

#[cfg(not(unix))]
fn display_herdr(
    _data: &[u8],
    _params: &ImageDisplayParams,
    _img_width: u32,
    _img_height: u32,
) -> io::Result<bool> {
    Ok(false)
}

fn can_display_to_stdout(protocol: ImageProtocol, stdout_is_terminal: bool) -> bool {
    stdout_is_terminal && protocol.is_supported()
}

/// Send a PNG to Herdr's pane graphics API. Unlike [`display_image`], this
/// client is intended for the TUI, where the caller owns the viewport
/// coordinates and must not require stdout to be a TTY.
#[cfg(unix)]
pub fn display_herdr_image(path: &Path, placement: HerdrImagePlacement) -> io::Result<bool> {
    let socket = match std::env::var("HERDR_SOCKET_PATH") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(false),
    };
    let pane_id = match std::env::var("HERDR_PANE_ID") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(false),
    };
    let data = std::fs::read(path)?;
    let (width, height) = get_image_dimensions(&data).unwrap_or((0, 0));
    if width == 0 || height == 0 {
        return Ok(false);
    }
    send_herdr_graphics(&socket, &pane_id, &data, width, height, Some(placement))
}

#[cfg(not(unix))]
pub fn display_herdr_image(_path: &Path, _placement: HerdrImagePlacement) -> io::Result<bool> {
    Ok(false)
}

/// Clear graphics previously placed by jcode in the current Herdr pane.
#[cfg(unix)]
pub fn clear_herdr_images() -> io::Result<bool> {
    let socket = match std::env::var("HERDR_SOCKET_PATH") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(false),
    };
    let pane_id = match std::env::var("HERDR_PANE_ID") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(false),
    };
    let request_id = NEXT_HERDR_REQUEST_ID.fetch_add(1, Ordering::Relaxed);
    let request = serde_json::json!({
        "id": format!("jcode:image:{}:{}", std::process::id(), request_id),
        "method": "pane.graphics.clear",
        "params": {"pane_id": pane_id},
    });
    herdr_request(&socket, request)
}

#[cfg(not(unix))]
pub fn clear_herdr_images() -> io::Result<bool> {
    Ok(false)
}

#[cfg(unix)]
fn send_herdr_graphics(
    socket: &str,
    pane_id: &str,
    data: &[u8],
    img_width: u32,
    img_height: u32,
    placement: Option<HerdrImagePlacement>,
) -> io::Result<bool> {
    let request_id = NEXT_HERDR_REQUEST_ID.fetch_add(1, Ordering::Relaxed);
    let (payload, payload_width, payload_height) =
        prepare_herdr_payload(data, img_width, img_height, placement)?;
    let request = build_herdr_set_request(
        request_id,
        pane_id,
        &payload,
        payload_width,
        payload_height,
        placement,
    );
    herdr_request(socket, request)
}

const HERDR_MAX_PAYLOAD_BYTES: usize = 900_000;

fn prepare_herdr_payload(
    data: &[u8],
    img_width: u32,
    img_height: u32,
    placement: Option<HerdrImagePlacement>,
) -> io::Result<(Vec<u8>, u32, u32)> {
    let Some(placement) = placement else {
        return Ok((data.to_vec(), img_width, img_height));
    };
    let max_width = placement.grid_cols.saturating_mul(8).max(8);
    let max_height = placement.grid_rows.saturating_mul(16).max(16);
    let image = image::load_from_memory(data)
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err.to_string()))?;
    let mut width = img_width;
    let mut height = img_height;
    let mut resized = image;
    if width > max_width || height > max_height {
        resized = resized.thumbnail(max_width, max_height);
        width = resized.width();
        height = resized.height();
    }
    loop {
        let rgba = resized.to_rgba8();
        let mut encoded = Vec::new();
        image::codecs::png::PngEncoder::new(&mut encoded)
            .write_image(
                rgba.as_raw(),
                width,
                height,
                image::ExtendedColorType::Rgba8,
            )
            .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err.to_string()))?;
        if encoded.len() <= HERDR_MAX_PAYLOAD_BYTES || width <= 32 || height <= 32 {
            return Ok((encoded, width, height));
        }
        width = (width as f32 * 0.8).round().max(32.0) as u32;
        height = (height as f32 * 0.8).round().max(32.0) as u32;
        resized = resized.resize(width, height, image::imageops::FilterType::Triangle);
    }
}

#[cfg(not(unix))]
fn send_herdr_graphics(
    _socket: &str,
    _pane_id: &str,
    _data: &[u8],
    _img_width: u32,
    _img_height: u32,
    _placement: Option<HerdrImagePlacement>,
) -> io::Result<bool> {
    Ok(false)
}

fn build_herdr_set_request(
    request_id: u64,
    pane_id: &str,
    data: &[u8],
    img_width: u32,
    img_height: u32,
    placement: Option<HerdrImagePlacement>,
) -> serde_json::Value {
    let mut params = serde_json::json!({
        "pane_id": pane_id,
        "format": "png",
        "data_base64": BASE64.encode(data),
        "image_width": img_width,
        "image_height": img_height,
    });
    if let Some(placement) = placement {
        params["placement"] = serde_json::json!({
            "viewport_col": placement.viewport_col,
            "viewport_row": placement.viewport_row,
            "grid_cols": placement.grid_cols,
            "grid_rows": placement.grid_rows,
        });
    }
    let request = serde_json::json!({
        "id": format!("jcode:image:{}:{}", std::process::id(), request_id),
        "method": "pane.graphics.set",
        "params": params,
    });
    request
}

#[cfg(unix)]
fn herdr_request(socket: &str, request: serde_json::Value) -> io::Result<bool> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(std::time::Duration::from_secs(2)))?;
    stream.write_all(request.to_string().as_bytes())?;
    stream.write_all(b"\n")?;
    stream.flush()?;
    let mut response = String::new();
    BufReader::new(stream).read_line(&mut response)?;
    if response.trim_start().starts_with('{') && !response.contains("\"error\"") {
        Ok(true)
    } else if response.contains("\"error\"") {
        Err(io::Error::other(response.trim().to_string()))
    } else {
        Ok(false)
    }
}

/// Get image dimensions from raw data
fn get_image_dimensions(data: &[u8]) -> Option<(u32, u32)> {
    // PNG: check signature and parse IHDR chunk
    if data.len() > 24 && &data[0..8] == b"\x89PNG\r\n\x1a\n" {
        let width = u32::from_be_bytes([data[16], data[17], data[18], data[19]]);
        let height = u32::from_be_bytes([data[20], data[21], data[22], data[23]]);
        return Some((width, height));
    }

    // JPEG: look for SOF0/SOF2 markers
    if data.len() > 2 && data[0] == 0xFF && data[1] == 0xD8 {
        let mut i = 2;
        while i + 9 < data.len() {
            if data[i] != 0xFF {
                i += 1;
                continue;
            }
            let marker = data[i + 1];
            // SOF0 (baseline) or SOF2 (progressive)
            if marker == 0xC0 || marker == 0xC2 {
                let height = u16::from_be_bytes([data[i + 5], data[i + 6]]) as u32;
                let width = u16::from_be_bytes([data[i + 7], data[i + 8]]) as u32;
                return Some((width, height));
            }
            // Skip to next marker
            if i + 3 < data.len() {
                let len = u16::from_be_bytes([data[i + 2], data[i + 3]]) as usize;
                i += 2 + len;
            } else {
                break;
            }
        }
    }

    // GIF: parse header
    if data.len() > 10 && (&data[0..6] == b"GIF87a" || &data[0..6] == b"GIF89a") {
        let width = u16::from_le_bytes([data[6], data[7]]) as u32;
        let height = u16::from_le_bytes([data[8], data[9]]) as u32;
        return Some((width, height));
    }

    // WebP: parse RIFF header
    if data.len() > 30 && &data[0..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        // VP8 chunk
        if &data[12..16] == b"VP8 " && data.len() > 30 {
            // VP8 bitstream starts at offset 23, dimensions at offset 26
            if data[23] == 0x9D && data[24] == 0x01 && data[25] == 0x2A {
                let width = (u16::from_le_bytes([data[26], data[27]]) & 0x3FFF) as u32;
                let height = (u16::from_le_bytes([data[28], data[29]]) & 0x3FFF) as u32;
                return Some((width, height));
            }
        }
        // VP8L (lossless)
        if &data[12..16] == b"VP8L" && data.len() > 25 {
            let bits = u32::from_le_bytes([data[21], data[22], data[23], data[24]]);
            let width = (bits & 0x3FFF) + 1;
            let height = ((bits >> 14) & 0x3FFF) + 1;
            return Some((width, height));
        }
    }

    None
}

/// Calculate display size maintaining aspect ratio
fn calculate_display_size(
    img_width: u32,
    img_height: u32,
    max_cols: u16,
    max_rows: u16,
) -> (u16, u16) {
    if img_width == 0 || img_height == 0 {
        return (max_cols.min(40), max_rows.min(20));
    }

    // Terminal cells are typically ~2:1 aspect ratio (taller than wide)
    // So we need to account for that when calculating display size
    let cell_aspect = 2.0; // height/width ratio of a terminal cell

    let img_aspect = img_width as f64 / img_height as f64;
    let max_width = max_cols as f64;
    let max_height = max_rows as f64 * cell_aspect; // Convert rows to "width units"

    let (display_width, display_height) = if img_aspect > max_width / max_height {
        // Image is wider than available space
        (max_width, max_width / img_aspect)
    } else {
        // Image is taller than available space
        (max_height * img_aspect, max_height)
    };

    (
        (display_width as u16).max(10),
        (display_height / cell_aspect) as u16, // Convert back to rows
    )
}

/// Display image using Kitty graphics protocol
fn display_kitty(
    data: &[u8],
    params: &ImageDisplayParams,
    img_width: u32,
    img_height: u32,
) -> io::Result<bool> {
    let (cols, rows) =
        calculate_display_size(img_width, img_height, params.max_cols, params.max_rows);

    // Encode image data as base64
    let encoded = BASE64.encode(data);

    let mut stdout = io::stdout().lock();

    // Kitty graphics protocol:
    // \x1b_G<key>=<value>,...;<payload>\x1b\\
    //
    // Keys:
    //   a=T - action: transmit and display
    //   f=100 - format: auto-detect
    //   c=<cols> - display width in cells
    //   r=<rows> - display height in cells
    //   m=1 - more data follows (chunked)
    //   m=0 - final chunk

    // Send in chunks (max 4096 bytes per chunk for safety)
    const CHUNK_SIZE: usize = 4096;
    let chunks: Vec<&str> = encoded
        .as_bytes()
        .chunks(CHUNK_SIZE)
        .map(|c| std::str::from_utf8(c).unwrap_or(""))
        .collect();

    for (i, chunk) in chunks.iter().enumerate() {
        let is_first = i == 0;
        let is_last = i == chunks.len() - 1;
        let more = if is_last { 0 } else { 1 };

        if is_first {
            // First chunk includes all parameters
            write!(
                stdout,
                "\x1b_Ga=T,f=100,c={},r={},m={};{}\x1b\\",
                cols, rows, more, chunk
            )?;
        } else {
            // Subsequent chunks only have m flag
            write!(stdout, "\x1b_Gm={};{}\x1b\\", more, chunk)?;
        }
    }

    // Newline after image
    writeln!(stdout)?;
    stdout.flush()?;

    Ok(true)
}

/// Display image using iTerm2 inline image protocol
fn display_iterm2(
    data: &[u8],
    path: &Path,
    params: &ImageDisplayParams,
    img_width: u32,
    img_height: u32,
) -> io::Result<bool> {
    let (cols, rows) =
        calculate_display_size(img_width, img_height, params.max_cols, params.max_rows);

    // Encode image data as base64
    let encoded = BASE64.encode(data);

    let filename = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "image".to_string());
    let filename_b64 = BASE64.encode(filename.as_bytes());

    let payload = iterm2_payload(&filename_b64, data.len(), cols, rows, &encoded, in_tmux());

    let mut stdout = io::stdout().lock();
    stdout.write_all(payload.as_bytes())?;
    // Newline after image
    writeln!(stdout)?;
    stdout.flush()?;

    Ok(true)
}

/// Build the iTerm2 inline-image escape sequence.
///
/// iTerm2 inline image protocol:
/// `ESC ]1337;File=name=<b64>;size=<n>;inline=1;width=<cols>;height=<rows>;preserveAspectRatio=1:<b64data> BEL`
///
/// Both `width` and `height` must be given in cells. With only `width`, iTerm2
/// derives the row count from the image's pixel aspect ratio, so it consumes a
/// different number of rows than we reserved and the surrounding output gets
/// pushed around or overwritten. Inside tmux the whole sequence also needs
/// passthrough wrapping, or tmux drops it.
fn iterm2_payload(
    filename_b64: &str,
    byte_len: usize,
    cols: u16,
    rows: u16,
    encoded: &str,
    is_tmux: bool,
) -> String {
    let (start, escape, end) = escape_tmux(is_tmux);
    format!(
        "{start}{escape}]1337;File=name={filename_b64};size={byte_len};inline=1;\
         width={cols};height={rows};preserveAspectRatio=1:{encoded}\x07{end}"
    )
}

/// Display image using Sixel graphics protocol
///
/// Uses ImageMagick's `convert` command to generate Sixel output.
/// This is the same approach used by image.nvim and other terminal image tools.
fn display_sixel(
    path: &Path,
    params: &ImageDisplayParams,
    img_width: u32,
    img_height: u32,
) -> io::Result<bool> {
    if !*HAS_IMAGEMAGICK {
        return Ok(false);
    }

    let (cols, rows) =
        calculate_display_size(img_width, img_height, params.max_cols, params.max_rows);

    // Calculate pixel dimensions based on typical terminal cell size
    // Assuming ~8px wide x 16px tall cells (common default)
    let pixel_width = (cols as u32) * 8;
    let pixel_height = (rows as u32) * 16;

    // Use ImageMagick to convert to Sixel
    // -geometry: resize to fit
    // -colors 256: limit palette for Sixel
    // sixel:-: output Sixel to stdout
    let output = Command::new("convert")
        .arg(path)
        .arg("-geometry")
        .arg(format!("{}x{}>", pixel_width, pixel_height))
        .arg("-colors")
        .arg("256")
        .arg("sixel:-")
        .output()?;

    if !output.status.success() {
        return Ok(false);
    }

    let mut stdout = io::stdout().lock();
    stdout.write_all(&output.stdout)?;
    writeln!(stdout)?;
    stdout.flush()?;

    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_protocol_detection() {
        // This test just verifies the detection doesn't panic
        let protocol = ImageProtocol::detect();
        println!("Detected protocol: {:?}", protocol);
    }

    #[test]
    fn redirected_stdout_disables_every_image_protocol() {
        for protocol in [
            ImageProtocol::Kitty,
            ImageProtocol::ITerm2,
            ImageProtocol::Sixel,
            ImageProtocol::None,
        ] {
            assert!(!can_display_to_stdout(protocol, false));
        }
    }

    #[test]
    fn terminal_stdout_still_allows_supported_image_protocols() {
        assert!(can_display_to_stdout(ImageProtocol::Kitty, true));
        assert!(can_display_to_stdout(ImageProtocol::ITerm2, true));
        assert!(can_display_to_stdout(ImageProtocol::Sixel, true));
        assert!(!can_display_to_stdout(ImageProtocol::None, true));
    }

    #[test]
    fn iterm2_payload_sets_both_cell_dimensions() {
        let payload = iterm2_payload("bmFtZQ==", 1234, 40, 12, "QUJD", false);
        assert!(payload.starts_with("\x1b]1337;File="), "{payload}");
        assert!(payload.contains("size=1234"), "{payload}");
        // Height in cells must be explicit so iTerm2 does not reflow rows.
        assert!(payload.contains("width=40;height=12"), "{payload}");
        assert!(payload.contains("preserveAspectRatio=1"), "{payload}");
        assert!(payload.ends_with("QUJD\x07"), "{payload}");
    }

    #[test]
    fn iterm2_payload_uses_tmux_passthrough_inside_tmux() {
        let payload = iterm2_payload("bmFtZQ==", 1, 4, 2, "QQ==", true);
        assert!(payload.starts_with("\x1bPtmux;\x1b\x1b]1337;"), "{payload}");
        assert!(payload.ends_with("\x07\x1b\\"), "{payload}");
    }

    #[test]
    fn iterm2_images_are_disabled_unless_opted_in() {
        // Default (no opt-in env var in test process): iTerm2 is treated as
        // having no usable image protocol.
        assert_eq!(iterm2_protocol(), ImageProtocol::None);
        assert!(!iterm2_images_enabled());
    }

    #[test]
    fn handterm_uses_kitty_graphics_protocol() {
        assert!(is_kitty_terminal_name("handterm"));
        assert!(is_kitty_terminal_name("HandTerm"));
    }

    #[test]
    fn herdr_requires_all_api_environment_values() {
        assert!(herdr_image_env(
            Some("1"),
            Some("1"),
            Some("/tmp/herdr.sock"),
            Some("w1:p1")
        ));
        assert!(!herdr_image_env(
            Some("1"),
            Some("1"),
            None,
            Some("w1:p1")
        ));
        assert!(!herdr_image_env(
            Some("1"),
            Some("0"),
            Some("/tmp/herdr.sock"),
            Some("w1:p1")
        ));
    }

    /// The Herdr graphics API acknowledges placements the attached client never
    /// paints, and the TUI suppresses its own in-buffer draw whenever that
    /// route is chosen, so an unacknowledged failure shows up as a black box.
    /// Routing must therefore stay off unless the user explicitly opts in.
    #[test]
    fn herdr_routing_requires_explicit_opt_in() {
        assert!(!herdr_image_env(
            None,
            Some("1"),
            Some("/tmp/herdr.sock"),
            Some("w1:p1")
        ));
        assert!(!herdr_image_env(
            Some("0"),
            Some("1"),
            Some("/tmp/herdr.sock"),
            Some("w1:p1")
        ));
        for enabled in ["1", "true", "YES", " on "] {
            assert!(
                herdr_images_opt_in(Some(enabled)),
                "expected {enabled:?} to enable Herdr image routing"
            );
        }
    }

    #[test]
    fn herdr_set_request_encodes_explicit_viewport_placement() {
        let request = build_herdr_set_request(
            7,
            "w1:p1",
            b"png",
            640,
            480,
            Some(HerdrImagePlacement {
                viewport_col: 4,
                viewport_row: 9,
                grid_cols: 80,
                grid_rows: 16,
            }),
        );
        assert_eq!(request["method"], "pane.graphics.set");
        assert_eq!(request["params"]["pane_id"], "w1:p1");
        assert_eq!(request["params"]["image_width"], 640);
        assert_eq!(request["params"]["placement"]["viewport_col"], 4);
        assert_eq!(request["params"]["placement"]["viewport_row"], 9);
        assert_eq!(request["params"]["placement"]["grid_cols"], 80);
        assert_eq!(request["params"]["placement"]["grid_rows"], 16);
    }

    #[test]
    fn herdr_payload_scales_to_placement_and_size_limit() {
        let image = image::RgbaImage::from_fn(1254, 1254, |x, y| {
            image::Rgba([(x % 251) as u8, (y % 251) as u8, 180, 255])
        });
        let mut source = Vec::new();
        image::codecs::png::PngEncoder::new(&mut source)
            .write_image(image.as_raw(), 1254, 1254, image::ExtendedColorType::Rgba8)
            .unwrap();
        let (payload, width, height) = prepare_herdr_payload(
            &source,
            1254,
            1254,
            Some(HerdrImagePlacement {
                viewport_col: 1,
                viewport_row: 23,
                grid_cols: 139,
                grid_rows: 16,
            }),
        )
        .unwrap();
        assert!(width <= 139 * 8);
        assert!(height <= 16 * 16);
        assert!(payload.len() <= HERDR_MAX_PAYLOAD_BYTES);
    }

    #[test]
    fn test_calculate_display_size() {
        // Wide image
        let (w, h) = calculate_display_size(1920, 1080, 80, 24);
        assert!(w <= 80);
        assert!(h <= 24);

        // Tall image
        let (w, h) = calculate_display_size(1080, 1920, 80, 24);
        assert!(w <= 80);
        assert!(h <= 24);

        // Square image
        let (w, h) = calculate_display_size(500, 500, 80, 24);
        assert!(w <= 80);
        assert!(h <= 24);
    }
}
