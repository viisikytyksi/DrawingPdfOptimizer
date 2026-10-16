//! DrawingPdfOptimizer native engine.
//!
//! A drop-in replacement for `python app/cli.py`: same arguments, same stdout
//! JSON, same stderr progress events and the same Shift_JIS processing log, so
//! `gui.ps1` can drive either engine.

mod decode;
mod log;
mod pipeline;

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use fax::encoder::Encoder;
use fax::{Color, VecWriter};
use lopdf::{Dictionary, Document, Object, ObjectId, Stream};

use decode::is_one_bit_dict;
use log::ProcessingLog;
use pipeline::Options;

const VERSION: &str = env!("CARGO_PKG_VERSION");

struct Args {
    inputs: Vec<PathBuf>,
    output_dir: Option<PathBuf>,
    dpi: u32,
    contrast: f32,
    threshold: i32,
    auto_contrast: bool,
    sharpen: bool,
    denoise: bool,
    include_small: bool,
    keep_metadata: bool,
    progress: bool,
    log_dir: Option<PathBuf>,
    log_fallback_dir: Option<PathBuf>,
    fullname: String,
    jobs: usize,
    timing: bool,
    resize_filter: pipeline::ResizeFilter,
}

fn usage() -> ! {
    eprintln!(
        "usage: dpo [options] FILE.pdf...\n\
         \x20 --output-dir DIR        出力先フォルダ\n\
         \x20 --dpi 200|300|400      出力解像度（既定 300）\n\
         \x20 --contrast F           コントラスト（既定 1.15）\n\
         \x20 --threshold N          黒を増減（既定 0）\n\
         \x20 --no-auto-contrast     自動コントラストを行わない\n\
         \x20 --no-sharpen           シャープ化を行わない\n\
         \x20 --denoise              ノイズ除去（孤立点除去）を行う\n\
         \x20 --include-small        小さい画像も処理する\n\
         \x20 --keep-metadata        PDFメタデータを残す\n\
         \x20 --progress             進捗を stderr へ JSON で出す\n\
         \x20 --log-dir DIR          処理ログの保存先\n\
         \x20 --log-fallback-dir DIR ログを書けないときの退避先\n\
         \x20 --fullname NAME        ログに記録する氏名\n\
         \x20 --jobs N               並列数（既定 0 = 自動）\n\
         \x20 --resize lanczos|bilinear|bicubic|nearest  縮小方式（既定 nearest。lanczos=Pillow互換）\n\
         \x20 --version              版数を表示"
    );
    std::process::exit(2)
}

fn parse_args() -> Args {
    let mut args = Args {
        inputs: Vec::new(),
        output_dir: None,
        dpi: 300,
        contrast: 1.15,
        threshold: 0,
        auto_contrast: true,
        sharpen: true,
        denoise: false,
        include_small: false,
        keep_metadata: false,
        progress: false,
        log_dir: None,
        log_fallback_dir: None,
        fullname: String::new(),
        jobs: 0,
        timing: false,
        resize_filter: pipeline::ResizeFilter::Nearest,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = || it.next().unwrap_or_else(|| usage());
        match arg.as_str() {
            "--output-dir" => args.output_dir = Some(PathBuf::from(value())),
            "--dpi" => {
                let dpi: u32 = value().parse().unwrap_or_else(|_| usage());
                if !matches!(dpi, 200 | 300 | 400) {
                    usage();
                }
                args.dpi = dpi;
            }
            "--contrast" => {
                args.contrast = value().parse::<f32>().unwrap_or(1.15).clamp(1.0, 1.5)
            }
            "--threshold" => {
                args.threshold = value().parse::<i32>().unwrap_or(0).clamp(-30, 30)
            }
            "--no-auto-contrast" => args.auto_contrast = false,
            "--no-sharpen" => args.sharpen = false,
            "--denoise" => args.denoise = true,
            "--include-small" => args.include_small = true,
            "--keep-metadata" => args.keep_metadata = true,
            "--progress" => args.progress = true,
            "--log-dir" => args.log_dir = Some(PathBuf::from(value())),
            "--log-fallback-dir" => args.log_fallback_dir = Some(PathBuf::from(value())),
            "--fullname" => args.fullname = value(),
            "--jobs" => args.jobs = value().parse().unwrap_or(0),
            "--timing" => args.timing = true,
            "--resize" => {
                let mode = value();
                args.resize_filter = match mode.as_str() {
                    "lanczos" => pipeline::ResizeFilter::Lanczos3,
                    "bilinear" => pipeline::ResizeFilter::Bilinear,
                    "bicubic" => pipeline::ResizeFilter::Bicubic,
                    "nearest" => pipeline::ResizeFilter::Nearest,
                    _ => usage(),
                };
            }
            "--version" | "-V" => {
                println!("{VERSION}");
                std::process::exit(0)
            }
            "-h" | "--help" => usage(),
            other => {
                if other.starts_with("--") {
                    usage()
                }
                args.inputs.push(PathBuf::from(other))
            }
        }
    }
    args
}

/// `cli.py::output_path` — `<stem>_2値化.pdf`, adding `_2`, `_3`, ... if taken.
fn output_path(source: &Path, folder: Option<&Path>) -> PathBuf {
    let dir = match folder {
        Some(dir) => dir.to_path_buf(),
        None => source.parent().map(Path::to_path_buf).unwrap_or_default(),
    };
    let stem = source
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut candidate = dir.join(format!("{stem}_2値化.pdf"));
    let mut number = 2;
    while candidate.exists() {
        candidate = dir.join(format!("{stem}_2値化_{number}.pdf"));
        number += 1;
    }
    candidate
}

struct ImageRef {
    id: ObjectId,
    width: u32,
    height: u32,
    data: Vec<u8>,
    dict: Dictionary,
}

struct ImageOutcome {
    index: usize,
    width: u32,
    height: u32,
    g4: Vec<u8>,
    _threshold: i32,
}

fn resolve_resources(doc: &Document, page_id: ObjectId) -> Result<Option<Dictionary>, String> {
    let mut current = Some(page_id);
    let mut guard = 0;
    while let Some(id) = current {
        guard += 1;
        if guard > 512 {
            return Err("page tree loop".into());
        }
        let Object::Dictionary(dict) = doc
            .get_object(id)
            .map_err(|e| format!("page object {id:?}: {e}"))?
        else {
            return Ok(None);
        };
        match dict.get(b"Resources") {
            Ok(Object::Reference(rid)) => {
                return match doc.get_object(*rid) {
                    Ok(Object::Dictionary(inner)) => Ok(Some(inner.clone())),
                    _ => Ok(None),
                }
            }
            Ok(Object::Dictionary(inner)) => return Ok(Some(inner.clone())),
            _ => {}
        }
        current = match dict.get(b"Parent") {
            Ok(Object::Reference(parent)) => Some(*parent),
            _ => None,
        };
    }
    Ok(None)
}

/// Collect image XObjects in page order, deduplicated by object id.
fn collect_images(doc: &Document) -> Result<Vec<ImageRef>, String> {
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for (_page_number, page_id) in doc.get_pages() {
        let Some(resources) = resolve_resources(doc, page_id)? else {
            continue;
        };
        let xobjects = match resources.get(b"XObject") {
            Ok(Object::Reference(id)) => match doc.get_object(*id) {
                Ok(Object::Dictionary(dict)) => dict,
                _ => continue,
            },
            Ok(Object::Dictionary(dict)) => dict,
            _ => continue,
        };
        for (_name, value) in xobjects.iter() {
            let Object::Reference(id) = value else { continue };
            if !seen.insert(*id) {
                continue;
            }
            let Ok(Object::Stream(stream)) = doc.get_object(*id) else {
                continue;
            };
            let subtype = stream
                .dict
                .get(b"Subtype")
                .and_then(Object::as_name)
                .unwrap_or(b"");
            if subtype != b"Image" {
                continue;
            }
            out.push(ImageRef {
                id: *id,
                width: decode::integer(&stream.dict, b"Width").unwrap_or(0).max(0) as u32,
                height: decode::integer(&stream.dict, b"Height").unwrap_or(0).max(0) as u32,
                data: stream.content.clone(),
                dict: stream.dict.clone(),
            });
        }
    }
    Ok(out)
}

/// Encode 1-bit pixels (1 byte/px, non-zero = white) as CCITT G4.
fn encode_g4(bits: &[u8], width: usize, height: usize) -> Vec<u8> {
    let writer = VecWriter::with_capacity(width * height / 8 + 64);
    let mut encoder = Encoder::new(writer);
    let mut line: Vec<Color> = Vec::with_capacity(width);
    for y in 0..height {
        line.clear();
        line.extend(bits[y * width..(y + 1) * width].iter().map(|&value| {
            if value != 0 {
                Color::White
            } else {
                Color::Black
            }
        }));
        encoder
            .encode_line(line.iter().copied(), width as u32)
            .expect("VecWriter is infallible");
    }
    encoder.finish().expect("VecWriter is infallible").finish()
}

/// Per-phase wall-clock totals, printed with `--timing`.
#[derive(Default)]
struct PhaseTotals {
    decode_ms: std::sync::atomic::AtomicU64,
    composite_ms: std::sync::atomic::AtomicU64,
    ops_ms: std::sync::atomic::AtomicU64,
    encode_ms: std::sync::atomic::AtomicU64,
    pixels: std::sync::atomic::AtomicU64,
}

/// Decode + prepare one image. Runs inside the rayon pool.
fn process_image(
    index: usize,
    image: &ImageRef,
    doc: &Document,
    opts: &Options,
    totals: &PhaseTotals,
) -> Result<ImageOutcome, String> {
    use std::sync::atomic::Ordering;
    let t_decode = Instant::now();
    let mut decoded = decode::decode(&image.dict, &image.data)?;
    totals
        .decode_ms
        .fetch_add(t_decode.elapsed().as_millis() as u64, Ordering::Relaxed);

    // pypdf composites an /SMask into the image before the Python engine sees it.
    if let Ok(Object::Reference(mask_id)) = image.dict.get(b"SMask") {
        if let Ok(Object::Stream(mask)) = doc.get_object(*mask_id) {
            if let Ok(mask_image) = decode::decode(&mask.dict, &mask.content) {
                let t_composite = Instant::now();
                decode::composite_over_white(&mut decoded.rgb, &mask_image);
                totals
                    .composite_ms
                    .fetch_add(t_composite.elapsed().as_millis() as u64, Ordering::Relaxed);
            }
        }
    }
    let t_ops = Instant::now();

    let (max_width, max_height) = pipeline::a3_pixel_box(decoded.width, decoded.height, opts.dpi);
    // The pipeline only ever reads the R channel, so extracting gray here keeps
    // the resized pixels byte-identical while resizing one channel instead of three.
    let gray: Vec<u8> = decoded.rgb.iter().step_by(3).copied().collect();
    let (gray, width, height) = pipeline::resize_gray(
        &gray,
        decoded.width,
        decoded.height,
        max_width,
        max_height,
        opts.resize_filter,
    );
    let binary = pipeline::binarize_gray(gray, width, height, opts);
    totals
        .ops_ms
        .fetch_add(t_ops.elapsed().as_millis() as u64, Ordering::Relaxed);
    totals
        .pixels
        .fetch_add((width as u64) * (height as u64), Ordering::Relaxed);
    let t_encode = Instant::now();
    let g4 = encode_g4(&binary.pixels, width as usize, height as usize);
    totals
        .encode_ms
        .fetch_add(t_encode.elapsed().as_millis() as u64, Ordering::Relaxed);
    Ok(ImageOutcome {
        index,
        width,
        height,
        g4,
        _threshold: binary.threshold,
    })
}

/// `engine.py::_is_signature_present` (simplified: /AcroForm /Fields with /FT /Sig).
fn has_signature(doc: &Document) -> bool {
    let Ok(catalog) = doc.catalog() else {
        return false;
    };
    let Ok(acroform) = catalog.get(b"AcroForm") else {
        return false;
    };
    let acroform = match acroform {
        Object::Reference(id) => match doc.get_object(*id) {
            Ok(Object::Dictionary(dict)) => dict.clone(),
            _ => return false,
        },
        Object::Dictionary(dict) => dict.clone(),
        _ => return false,
    };
    let mut stack: Vec<Object> = match acroform.get(b"Fields") {
        Ok(Object::Array(items)) => items.clone(),
        _ => return false,
    };
    while let Some(field) = stack.pop() {
        let field = match field {
            Object::Reference(id) => match doc.get_object(id) {
                Ok(Object::Dictionary(dict)) => Object::Dictionary(dict.clone()),
                _ => continue,
            },
            other => other,
        };
        if let Object::Dictionary(dict) = &field {
            if dict.get(b"FT").and_then(Object::as_name).unwrap_or(b"") == b"Sig" {
                return true;
            }
            if let Ok(Object::Array(kids)) = dict.get(b"Kids") {
                stack.extend(kids.iter().cloned());
            }
        }
    }
    false
}

/// Remove the trailer /Info, trailer /ID and the catalog XMP /Metadata stream.
fn strip_metadata(doc: &mut Document) -> bool {
    let mut changed = false;
    if doc.trailer.remove(b"Info").is_some() {
        changed = true;
    }
    if doc.trailer.remove(b"ID").is_some() {
        changed = true;
    }
    if let Ok(catalog) = doc.catalog_mut() {
        if catalog.remove(b"Metadata").is_some() {
            changed = true;
        }
    }
    changed
}

fn canonical(obj: &Object, out: &mut Vec<u8>) {
    match obj {
        Object::Name(name) => {
            out.push(1);
            out.extend_from_slice(name);
        }
        Object::Integer(number) => {
            out.push(2);
            out.extend_from_slice(&number.to_le_bytes());
        }
        Object::Real(number) => {
            out.push(3);
            out.extend_from_slice(&number.to_le_bytes());
        }
        Object::Boolean(flag) => out.push(if *flag { 4 } else { 5 }),
        Object::Array(items) => {
            out.push(6);
            for item in items {
                canonical(item, out);
            }
        }
        Object::Dictionary(dict) => {
            out.push(7);
            for (key, value) in dict.iter() {
                out.extend_from_slice(key);
                out.push(0xff);
                canonical(value, out);
            }
        }
        Object::Reference(id) => {
            out.push(8);
            out.extend_from_slice(&id.0.to_le_bytes());
            out.extend_from_slice(&id.1.to_le_bytes());
        }
        _ => out.push(9),
    }
}

fn replace_refs(obj: &mut Object, remap: &HashMap<ObjectId, ObjectId>) {
    match obj {
        Object::Reference(id) => {
            if let Some(&keep) = remap.get(id) {
                *id = keep;
            }
        }
        Object::Array(items) => {
            for item in items.iter_mut() {
                replace_refs(item, remap);
            }
        }
        Object::Dictionary(dict) => {
            for (_key, value) in dict.iter_mut() {
                replace_refs(value, remap);
            }
        }
        Object::Stream(stream) => {
            for (_key, value) in stream.dict.iter_mut() {
                replace_refs(value, remap);
            }
        }
        _ => {}
    }
}

/// Merge image XObjects with identical dictionaries and stream bytes
/// (the equivalent of pypdf's `compress_identical_objects()`).
/// Drop objects that nothing references any more.
///
/// pypdf does the same when it writes, which is why the Python output has no
/// trace of the detached /SMask images; lopdf would otherwise write them and
/// blow the file up (6 masks of a scanned sheet are ~54MB).
fn sweep_unreferenced(doc: &mut Document) -> usize {
    let mut reachable: std::collections::HashSet<ObjectId> = std::collections::HashSet::new();
    let mut stack: Vec<ObjectId> = Vec::new();
    let mut push = |obj: &Object, stack: &mut Vec<ObjectId>| collect_refs(obj, stack);

    for (_key, value) in doc.trailer.iter() {
        push(value, &mut stack);
    }
    for id in doc.get_pages().values() {
        stack.push(*id);
    }
    while let Some(id) = stack.pop() {
        if !reachable.insert(id) {
            continue;
        }
        let Ok(object) = doc.get_object(id) else {
            continue;
        };
        match object {
            Object::Array(items) => {
                for item in items.clone() {
                    push(&item, &mut stack);
                }
            }
            Object::Dictionary(dict) => {
                for (_key, value) in dict.clone().iter() {
                    push(value, &mut stack);
                }
            }
            Object::Stream(stream) => {
                for (_key, value) in stream.dict.clone().iter() {
                    push(value, &mut stack);
                }
            }
            _ => {}
        }
    }
    let before = doc.objects.len();
    doc.objects.retain(|id, _| reachable.contains(id));
    before - doc.objects.len()
}

fn collect_refs(obj: &Object, stack: &mut Vec<ObjectId>) {
    match obj {
        Object::Reference(id) => stack.push(*id),
        Object::Array(items) => {
            for item in items {
                collect_refs(item, stack);
            }
        }
        Object::Dictionary(dict) => {
            for (_key, value) in dict.iter() {
                collect_refs(value, stack);
            }
        }
        Object::Stream(stream) => {
            for (_key, value) in stream.dict.iter() {
                collect_refs(value, stack);
            }
        }
        _ => {}
    }
}

fn dedup_identical_images(doc: &mut Document, ids: &[ObjectId]) -> usize {
    let mut seen: HashMap<Vec<u8>, ObjectId> = HashMap::new();
    let mut remap: HashMap<ObjectId, ObjectId> = HashMap::new();
    for &id in ids {
        let Some(Object::Stream(stream)) = doc.objects.get(&id) else {
            continue;
        };
        let mut key = Vec::with_capacity(stream.content.len() + 128);
        canonical(&Object::Dictionary(stream.dict.clone()), &mut key);
        key.extend_from_slice(&stream.content);
        match seen.get(&key) {
            Some(&keep) => {
                remap.insert(id, keep);
            }
            None => {
                seen.insert(key, id);
            }
        }
    }
    if remap.is_empty() {
        return 0;
    }
    for (_id, obj) in doc.objects.iter_mut() {
        replace_refs(obj, &remap);
    }
    for id in remap.keys() {
        doc.objects.remove(id);
    }
    remap.len()
}

#[derive(Default)]
struct FileStats {
    total_images: usize,
    converted: usize,
    skipped_small: usize,
    skipped_unsupported: usize,
    warnings: Vec<String>,
    bytes_before: u64,
    bytes_after: u64,
    metadata_stripped: bool,
}

fn process_file(
    path: &Path,
    args: &Args,
    progress: &mut dyn FnMut(usize, usize),
) -> Result<FileStats, String> {
    let mut stats = FileStats {
        bytes_before: fs::metadata(path).map(|m| m.len()).unwrap_or(0),
        ..Default::default()
    };
    let mut doc = Document::load(path).map_err(|e| format!("PDFを開けません: {e}"))?;

    if has_signature(&doc) {
        stats.warnings.push("電子署名は保存時に無効になります。".to_string());
    }
    if !args.keep_metadata {
        stats.metadata_stripped = strip_metadata(&mut doc);
    }

    let images = collect_images(&doc)?;
    let opts = Options {
        dpi: args.dpi,
        auto_contrast: args.auto_contrast,
        contrast: args.contrast,
        sharpen: args.sharpen,
        denoise: args.denoise,
        resize_filter: args.resize_filter,
        ..Default::default()
    };

    let mut candidates: Vec<ImageRef> = Vec::new();
    for image in images {
        stats.total_images += 1;
        if !args.include_small
            && image.width.max(image.height) < opts.min_long_side
            && (image.width as u64 * image.height as u64) < opts.min_pixels
        {
            stats.skipped_small += 1;
            continue;
        }
        let (max_width, max_height) = pipeline::a3_pixel_box(image.width, image.height, opts.dpi);
        if is_one_bit_dict(&image.dict)
            && image.width <= max_width
            && image.height <= max_height
        {
            continue; // already 1-bit and small enough
        }
        candidates.push(image);
    }

    let total = candidates.len();
    let thread_pool = rayon::ThreadPoolBuilder::new()
        .num_threads(if args.jobs > 0 {
            args.jobs
        } else {
            rayon::current_num_threads()
        })
        .build()
        .map_err(|e| e.to_string())?;
    let totals = PhaseTotals::default();
    let outcomes: Vec<Result<ImageOutcome, String>> = thread_pool.install(|| {
        use rayon::prelude::*;
        candidates
            .par_iter()
            .enumerate()
            .map(|(index, image)| process_image(index + 1, image, &doc, &opts, &totals))
            .collect()
    });
    if args.timing {
        use std::sync::atomic::Ordering;
        eprintln!(
            "{}\n  timing: decode={}ms composite={}ms ops={}ms g4={}ms pixels={} ({:.2} Mpx)",
            pipeline::timing::report(),
            totals.decode_ms.load(Ordering::Relaxed),
            totals.composite_ms.load(Ordering::Relaxed),
            totals.ops_ms.load(Ordering::Relaxed),
            totals.encode_ms.load(Ordering::Relaxed),
            totals.pixels.load(Ordering::Relaxed),
            totals.pixels.load(Ordering::Relaxed) as f64 / 1_000_000.0,
        );
    }

    let mut replaced: Vec<ObjectId> = Vec::with_capacity(candidates.len());
    for (position, outcome) in outcomes.into_iter().enumerate() {
        progress(position + 1, total);
        let image = &candidates[position];
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                stats.skipped_unsupported += 1;
                stats
                    .warnings
                    .push(format!("画像 {}: {error}", position + 1));
                continue;
            }
        };
        let mut dict = Dictionary::new();
        dict.set("Type", Object::Name(b"XObject".to_vec()));
        dict.set("Subtype", Object::Name(b"Image".to_vec()));
        dict.set("Width", Object::Integer(outcome.width as i64));
        dict.set("Height", Object::Integer(outcome.height as i64));
        dict.set("BitsPerComponent", Object::Integer(1));
        dict.set("ColorSpace", Object::Name(b"DeviceGray".to_vec()));
        dict.set("Filter", Object::Name(b"CCITTFaxDecode".to_vec()));
        let mut parms = Dictionary::new();
        parms.set("K", Object::Integer(-1));
        parms.set("Columns", Object::Integer(outcome.width as i64));
        parms.set("Rows", Object::Integer(outcome.height as i64));
        parms.set("BlackIs1", Object::Boolean(false));
        dict.set("DecodeParms", Object::Dictionary(parms));
        doc.objects
            .insert(image.id, Object::Stream(Stream::new(dict, outcome.g4)));
        replaced.push(image.id);
        stats.converted += 1;
        let _ = outcome.index;
    }

    if !replaced.is_empty() {
        dedup_identical_images(&mut doc, &replaced);
    }
    sweep_unreferenced(&mut doc);

    let target = output_path(path, args.output_dir.as_deref());
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let temporary = target.with_extension("pdf.partial");
    doc.save(&temporary)
        .map_err(|e| format!("PDFを保存できません: {e}"))?;
    fs::rename(&temporary, &target).map_err(|e| e.to_string())?;
    stats.bytes_after = fs::metadata(&target).map(|m| m.len()).unwrap_or(0);
    Ok(stats)
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

struct ProgressEmitter {
    enabled: bool,
}

impl ProgressEmitter {
    /// `{"type": event, "k": "v", ..., "n": 123, ...}` — the same shape cli.py emits.
    fn emit(&self, event: &str, strings: &[(&str, String)], numbers: &[(&str, i64)]) {
        if !self.enabled {
            return;
        }
        let mut line = format!("{{\"type\": \"{event}\"");
        for (key, value) in strings {
            line.push_str(&format!(", \"{key}\": \"{}\"", json_escape(value)));
        }
        for (key, value) in numbers {
            line.push_str(&format!(", \"{key}\": {value}"));
        }
        line.push('}');
        eprintln!("{line}");
    }
}

fn main() {
    // このビルドは x86-64-v3 (AVX2/FMA) を前提とする。対応しないCPUでは
    // 不正命令で落ちる代わりに、ここで明示的に止める。
    if !std::arch::is_x86_feature_detected!("avx2") {
        eprintln!("エラー: このバイナリはAVX2(x86-64-v3)対応CPUが必要です");
        std::process::exit(1);
    }
    let args = parse_args();
    if args.inputs.is_empty() {
        usage();
    }
    let progress = ProgressEmitter {
        enabled: args.progress,
    };
    let mut processing_log = ProcessingLog::new(
        args.log_dir.clone(),
        args.log_fallback_dir.clone(),
        args.output_dir.clone(),
        args.inputs[0].clone(),
        VERSION,
        &args.fullname,
        args.dpi,
        args.progress,
    );

    let total_files = args.inputs.len();
    processing_log.write_run_start(total_files);

    let started = Instant::now();
    let mut results: Vec<String> = Vec::new();
    let mut failures: Vec<(String, String)> = Vec::new();

    for (index, source) in args.inputs.iter().enumerate() {
        let file_number = index + 1;
        let name = source
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        progress.emit(
            "file_start",
            &[("name", name.clone())],
            &[("file", file_number as i64), ("total_files", total_files as i64)],
        );

        let file_started = Instant::now();
        let target = output_path(source, args.output_dir.as_deref());
        let mut progress_cb = |current: usize, total: usize| {
            let fraction = if total > 0 {
                current as f64 / total as f64
            } else {
                0.0
            };
            progress.emit(
                "progress",
                &[
                    ("name", name.clone()),
                    ("message", format!("画像 {current}/{total} を処理中")),
                ],
                &[
                    ("file", file_number as i64),
                    ("total_files", total_files as i64),
                    ("image", current as i64),
                    ("total_images", total as i64),
                    ("fraction", (fraction * 10000.0).round() as i64),
                ],
            );
        };

        match process_file(source, &args, &mut progress_cb) {
            Ok(stats) => {
                let elapsed = file_started.elapsed().as_secs_f64();
                results.push(format!(
                    "{{\"input\": \"{}\", \"output\": \"{}\", \"total_images\": {}, \
                     \"converted_images\": {}, \"skipped_small\": {}, \"skipped_inline\": 0, \
                     \"skipped_unsupported\": {}, \"bytes_before\": {}, \"bytes_after\": {}, \
                     \"metadata_stripped\": {}, \"warnings\": [{}]}}",
                    json_escape(&source.display().to_string()),
                    json_escape(&target.display().to_string()),
                    stats.total_images,
                    stats.converted,
                    stats.skipped_small,
                    stats.skipped_unsupported,
                    stats.bytes_before,
                    stats.bytes_after,
                    stats.metadata_stripped,
                    stats
                        .warnings
                        .iter()
                        .map(|warning| format!("\"{}\"", json_escape(warning)))
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
                processing_log.write_file_row(
                    source,
                    "ok",
                    "",
                    stats.total_images,
                    stats.converted,
                    stats.skipped_small,
                    stats.skipped_unsupported,
                    stats.bytes_after,
                    elapsed,
                );
                progress.emit(
                    "file_done",
                    &[("name", name.clone())],
                    &[("file", file_number as i64), ("total_files", total_files as i64)],
                );
            }
            Err(error) => {
                let elapsed = file_started.elapsed().as_secs_f64();
                failures.push((source.display().to_string(), error.clone()));
                processing_log.write_file_row(source, "error", &error, 0, 0, 0, 0, 0, elapsed);
                progress.emit(
                    "file_error",
                    &[("name", name.clone()), ("error", error.clone())],
                    &[("file", file_number as i64)],
                );
            }
        }
    }

    processing_log.write_run_done(
        total_files,
        results.len(),
        failures.len(),
        started.elapsed().as_secs_f64(),
        &failures,
    );
    progress.emit(
        "all_done",
        &[],
        &[
            ("total_files", total_files as i64),
            ("failed", failures.len() as i64),
        ],
    );

    let failure_json: Vec<String> = failures
        .iter()
        .map(|(input, error)| {
            format!(
                "{{\"input\": \"{}\", \"error\": \"{}\"}}",
                json_escape(input),
                json_escape(error)
            )
        })
        .collect();
    println!(
        "{{\"results\": [{}], \"failures\": [{}]}}",
        results.join(", "),
        failure_json.join(", ")
    );
}
