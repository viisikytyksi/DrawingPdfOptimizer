//! Faithful port of DrawingPdfOptimizer's Pillow-based binarization pipeline
//! (`app/engine.py::_prepare_image`).
//!
//! The Pillow primitives used by the Python implementation are re-implemented
//! here bit-for-bit from the Pillow 12.3.0 C sources (BoxBlur.c, UnsharpMask.c,
//! Blend.c) so that the Rust output can be compared pixel-by-pixel.

/// Coarse phase accounting, printed by the CLI with `--timing`.
pub mod timing {
    use std::sync::atomic::{AtomicU64, Ordering};

    pub static BLUR: AtomicU64 = AtomicU64::new(0);
    pub static SUBTRACT: AtomicU64 = AtomicU64::new(0);
    pub static AUTOCONTRAST: AtomicU64 = AtomicU64::new(0);
    pub static CONTRAST: AtomicU64 = AtomicU64::new(0);
    pub static UNSHARP: AtomicU64 = AtomicU64::new(0);
    pub static OTSU: AtomicU64 = AtomicU64::new(0);
    pub static BINARIZE: AtomicU64 = AtomicU64::new(0);
    pub static DESPECKLE: AtomicU64 = AtomicU64::new(0);
    pub static RESIZE: AtomicU64 = AtomicU64::new(0);
    pub static RESIZE_REDUCE: AtomicU64 = AtomicU64::new(0);
    pub static RESIZE_H: AtomicU64 = AtomicU64::new(0);
    pub static RESIZE_V: AtomicU64 = AtomicU64::new(0);

    pub fn add(counter: &AtomicU64, elapsed: std::time::Duration) {
        counter.fetch_add(elapsed.as_millis() as u64, Ordering::Relaxed);
    }

    pub fn report() -> String {
        format!(
            "pipeline: resize={}ms blur={}ms subtract={}ms autocontrast={}ms contrast={}ms unsharp={}ms otsu={}ms binarize={}ms despeckle={}ms resize_reduce={}ms resize_h={}ms resize_v={}ms",
            RESIZE.load(Ordering::Relaxed),
            BLUR.load(Ordering::Relaxed),
            SUBTRACT.load(Ordering::Relaxed),
            AUTOCONTRAST.load(Ordering::Relaxed),
            CONTRAST.load(Ordering::Relaxed),
            UNSHARP.load(Ordering::Relaxed),
            OTSU.load(Ordering::Relaxed),
            BINARIZE.load(Ordering::Relaxed),
            DESPECKLE.load(Ordering::Relaxed),
            RESIZE_REDUCE.load(Ordering::Relaxed),
            RESIZE_H.load(Ordering::Relaxed),
            RESIZE_V.load(Ordering::Relaxed),
        )
    }
}

pub const MIN_LONG_SIDE: u32 = 512;
pub const MIN_PIXELS: u64 = 1_000_000;

#[derive(Clone, Debug)]
pub struct Options {
    pub dpi: u32,
    pub auto_contrast: bool,
    pub contrast: f32,
    pub sharpen: bool,
    pub denoise: bool,
    pub min_long_side: u32,
    pub min_pixels: u64,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            dpi: 300,
            auto_contrast: true,
            contrast: 1.15,
            sharpen: true,
            denoise: true,
            min_long_side: MIN_LONG_SIDE,
            min_pixels: MIN_PIXELS,
        }
    }
}

/// `engine.py::_a3_pixel_box`
pub fn a3_pixel_box(w: u32, h: u32, dpi: u32) -> (u32, u32) {
    let short_side = (11.6929_f64 * dpi as f64).round() as u32;
    let long_side = (16.5354_f64 * dpi as f64).round() as u32;
    if w >= h {
        (long_side, short_side)
    } else {
        (short_side, long_side)
    }
}

#[inline]
fn clip8(v: i32) -> u8 {
    if v >= 255 {
        255
    } else if v <= 0 {
        0
    } else {
        v as u8
    }
}

/// `_gaussian_blur_radius()` from Pillow's BoxBlur.c: the fractional box radius
/// used to approximate a gaussian of the given sigma with `passes` box passes.
fn gaussian_blur_radius(radius: f32, passes: i32) -> f32 {
    let sigma2 = radius * radius / passes as f32;
    // float L = sqrt(12.0 * sigma2 + 1.0);  (double expression, rounded to float)
    let l_big = ((12.0f64 * sigma2 as f64 + 1.0).sqrt()) as f32;
    let l = (((l_big as f64) - 1.0) / 2.0).floor() as f32;
    let mut a: f32 = (2.0 * l + 1.0) * (l * (l + 1.0) - 3.0 * sigma2);
    a /= 6.0 * (sigma2 - (l + 1.0) * (l + 1.0));
    l + a
}

/// One horizontal box-blur pass, `ImagingLineBoxBlur8` + `ImagingHorizontalBoxBlur`
/// from BoxBlur.c (8-bit single band path).
fn box_blur_h(src: &[u8], w: usize, h: usize, out: &mut [u8], float_radius: f32) {
    assert!(w > 0 && h > 0);
    let radius = float_radius as i64 as u32;
    let ww: u32 = (((1u32 << 24) as f32) / (float_radius * 2.0 + 1.0)) as u32;
    let fw: u32 = ((1u32 << 24) - (radius * 2 + 1) * ww) / 2;

    let lastx = w - 1;
    let edge_a = std::cmp::min(radius as usize + 1, w);
    let edge_b = w.saturating_sub(radius as usize + 1);

    for y in 0..h {
        let line = &src[y * w..y * w + w];
        let o = &mut out[y * w..y * w + w];

        // acc for the pixel just left of the row (edge replication)
        let mut acc: u32 = (line[0] as u32).wrapping_mul(radius + 1);
        for x in 0..edge_a.saturating_sub(1) {
            acc = acc.wrapping_add(line[x] as u32);
        }
        acc = acc.wrapping_add(
            (line[lastx] as u32).wrapping_mul((radius as i64 - edge_a as i64 + 1) as u32),
        );

        macro_rules! save {
            ($x:expr, $bulk:expr) => {{
                o[$x] = ((($bulk).wrapping_add(1 << 23)) >> 24) as u8;
            }};
        }

        if edge_a <= edge_b {
            for x in 0..edge_a {
                acc = acc
                    .wrapping_add(line[x + radius as usize] as u32)
                    .wrapping_sub(line[0] as u32);
                let bulk = acc
                    .wrapping_mul(ww)
                    .wrapping_add((line[0] as u32 + line[x + radius as usize + 1] as u32).wrapping_mul(fw));
                save!(x, bulk);
            }
            for x in edge_a..edge_b {
                acc = acc
                    .wrapping_add(line[x + radius as usize] as u32)
                    .wrapping_sub(line[x - radius as usize - 1] as u32);
                let bulk = acc.wrapping_mul(ww).wrapping_add(
                    (line[x - radius as usize - 1] as u32 + line[x + radius as usize + 1] as u32)
                        .wrapping_mul(fw),
                );
                save!(x, bulk);
            }
            for x in edge_b..=lastx {
                acc = acc
                    .wrapping_add(line[lastx] as u32)
                    .wrapping_sub(line[x - radius as usize - 1] as u32);
                let bulk = acc.wrapping_mul(ww).wrapping_add(
                    (line[x - radius as usize - 1] as u32 + line[lastx] as u32).wrapping_mul(fw),
                );
                save!(x, bulk);
            }
        } else {
            for x in 0..edge_b {
                acc = acc
                    .wrapping_add(line[x + radius as usize] as u32)
                    .wrapping_sub(line[0] as u32);
                let bulk = acc
                    .wrapping_mul(ww)
                    .wrapping_add((line[0] as u32 + line[x + radius as usize + 1] as u32).wrapping_mul(fw));
                save!(x, bulk);
            }
            for x in edge_b..edge_a {
                acc = acc
                    .wrapping_add(line[lastx] as u32)
                    .wrapping_sub(line[0] as u32);
                let bulk = acc
                    .wrapping_mul(ww)
                    .wrapping_add((line[0] as u32 + line[lastx] as u32).wrapping_mul(fw));
                save!(x, bulk);
            }
            for x in edge_a..=lastx {
                acc = acc
                    .wrapping_add(line[lastx] as u32)
                    .wrapping_sub(line[x - radius as usize - 1] as u32);
                let bulk = acc.wrapping_mul(ww).wrapping_add(
                    (line[x - radius as usize - 1] as u32 + line[lastx] as u32).wrapping_mul(fw),
                );
                save!(x, bulk);
            }
        }
    }
}

/// Plain (non-interpolating) transpose, like `ImagingTranspose`.
fn transpose(src: &[u8], w: usize, h: usize) -> (Vec<u8>, usize, usize) {
    let mut out = vec![0u8; w * h];
    for y in 0..h {
        for x in 0..w {
            out[x * h + y] = src[y * w + x];
        }
    }
    (out, h, w)
}

/// `ImagingGaussianBlur(imOut, imIn, radius, radius, 3)` — 3 box passes per axis.
pub fn gaussian_blur(src: &[u8], w: usize, h: usize, radius: f32) -> Vec<u8> {
    let fr = gaussian_blur_radius(radius, 3);
    let mut buf = vec![0u8; w * h];
    let mut tmp = vec![0u8; w * h];
    // horizontal passes (first pass reads src, following passes read previous result)
    box_blur_h(src, w, h, &mut buf, fr);
    for _ in 1..3 {
        tmp.copy_from_slice(&buf);
        box_blur_h(&tmp, w, h, &mut buf, fr);
    }
    // vertical passes, done on a transposed buffer exactly like libImaging
    let (mut t, tw, th) = transpose(&buf, w, h);
    let mut to = vec![0u8; t.len()];
    box_blur_h(&t, tw, th, &mut to, fr);
    for _ in 1..3 {
        let prev = to.clone();
        box_blur_h(&prev, tw, th, &mut to, fr);
    }
    t.copy_from_slice(&to);
    let (res, rw, rh) = transpose(&t, tw, th);
    debug_assert_eq!((rw, rh), (w, h));
    res
}

/// `ImageChops.subtract(gray, background, offset=128)` (8-bit, scale=1).
pub fn subtract_offset(a: &[u8], b: &[u8], offset: i32) -> Vec<u8> {
    a.iter()
        .zip(b.iter())
        .map(|(&x, &y)| clip8(x as i32 - y as i32 + offset))
        .collect()
}

/// `ImageOps.autocontrast(image, cutoff=0.5)` (PIL/ImageOps.py, Pillow 12.x).
pub fn autocontrast(src: &[u8], cutoff: (f32, f32)) -> Vec<u8> {
    let mut h = [0u64; 256];
    for &v in src {
        h[v as usize] += 1;
    }
    let n: u64 = src.len() as u64;
    let mut cut = (n as f64 * cutoff.0 as f64 / 100.0).floor() as u64;
    for lo in 0..256 {
        if cut > h[lo] {
            cut -= h[lo];
            h[lo] = 0;
        } else {
            h[lo] -= cut;
            cut = 0;
        }
        if cut == 0 {
            break;
        }
    }
    let mut cut = (n as f64 * cutoff.1 as f64 / 100.0).floor() as u64;
    for hi in (0..256).rev() {
        if cut > h[hi] {
            cut -= h[hi];
            h[hi] = 0;
        } else {
            h[hi] -= cut;
            cut = 0;
        }
        if cut == 0 {
            break;
        }
    }
    let mut lo = 0usize;
    for i in 0..256 {
        if h[i] != 0 {
            lo = i;
            break;
        }
    }
    let mut hi = 255usize;
    for i in (0..256).rev() {
        if h[i] != 0 {
            hi = i;
            break;
        }
    }
    if hi <= lo {
        return src.to_vec();
    }
    let scale = 255.0f64 / (hi - lo) as f64;
    let offset = -(lo as f64) * scale;
    let mut lut = [0u8; 256];
    for ix in 0..256 {
        let v = (ix as f64 * scale + offset).trunc() as i64;
        lut[ix] = v.clamp(0, 255) as u8;
    }
    src.iter().map(|&v| lut[v as usize]).collect()
}

/// `ImageEnhance.Contrast(image).enhance(factor)` on an "L" image:
/// `Image.blend(degenerate, image, factor)` with `degenerate = mean`.
pub fn contrast_enhance(src: &[u8], factor: f32) -> Vec<u8> {
    let n = src.len() as f64;
    let sum: f64 = src.iter().map(|&v| v as f64).sum();
    let mean = (sum / n + 0.5).trunc() as i32;
    src.iter()
        .map(|&v| {
            let temp = mean as f32 + factor * (v as f32 - mean as f32);
            if temp <= 0.0 {
                0
            } else if temp >= 255.0 {
                255
            } else {
                temp as u8
            }
        })
        .collect()
}

/// `ImageFilter.UnsharpMask(radius, percent, threshold)` (UnsharpMask.c).
pub fn unsharp_mask(src: &[u8], w: usize, h: usize, radius: f32, percent: i32, threshold: i32) -> Vec<u8> {
    let blurred = gaussian_blur(src, w, h, radius);
    src.iter()
        .zip(blurred.iter())
        .map(|(&v, &b)| {
            let diff = v as i32 - b as i32;
            if diff.abs() > threshold {
                clip8(v as i32 + diff * percent / 100)
            } else {
                v
            }
        })
        .collect()
}

/// `engine.py::_otsu_threshold`
pub fn otsu_threshold(src: &[u8]) -> i32 {
    let mut histogram = [0u64; 256];
    for &v in src {
        histogram[v as usize] += 1;
    }
    let total = src.len() as u64;
    let weighted_total: u64 = histogram
        .iter()
        .enumerate()
        .map(|(i, &c)| i as u64 * c)
        .sum();
    let mut background_weight: u64 = 0;
    let mut background_sum: u64 = 0;
    let mut best_variance = -1.0f64;
    let mut best_threshold = 127i32;
    for threshold in 0..256usize {
        let count = histogram[threshold];
        background_weight += count;
        if background_weight == 0 {
            continue;
        }
        let foreground_weight = total - background_weight;
        if foreground_weight == 0 {
            break;
        }
        background_sum += threshold as u64 * count;
        let background_mean = background_sum as f64 / background_weight as f64;
        let foreground_mean =
            (weighted_total - background_sum) as f64 / foreground_weight as f64;
        let variance = (background_weight as f64 * foreground_weight as f64)
            * ((background_mean - foreground_mean) * (background_mean - foreground_mean));
        if variance > best_variance {
            best_variance = variance;
            best_threshold = threshold as i32;
        }
    }
    best_threshold
}

pub struct Binary {
    pub w: u32,
    pub h: u32,
    /// one byte per pixel: 1 = white, 0 = black (Pillow mode "1" convention)
    pub pixels: Vec<u8>,
    pub threshold: i32,
}

/// Whole `_prepare_image()` pipeline for a single-channel ("L") input.
/// `gray` is consumed as the working channel (R of an RGB source is identical).
pub fn binarize_gray(mut gray: Vec<u8>, w: u32, h: u32, opts: &Options) -> Binary {
    let (wu, hu) = (w as usize, h as usize);

    // background = gray.filter(GaussianBlur(radius=25)); gray = subtract(gray, background, 128)
    let t = std::time::Instant::now();
    let background = gaussian_blur(&gray, wu, hu, 25.0);
    timing::add(&timing::BLUR, t.elapsed());
    let t = std::time::Instant::now();
    gray = subtract_offset(&gray, &background, 128);
    timing::add(&timing::SUBTRACT, t.elapsed());

    if opts.auto_contrast {
        let t = std::time::Instant::now();
        gray = autocontrast(&gray, (0.5, 0.5));
        timing::add(&timing::AUTOCONTRAST, t.elapsed());
    }
    if opts.contrast != 1.0 {
        let t = std::time::Instant::now();
        gray = contrast_enhance(&gray, opts.contrast);
        timing::add(&timing::CONTRAST, t.elapsed());
    }
    if opts.sharpen {
        let t = std::time::Instant::now();
        gray = unsharp_mask(&gray, wu, hu, 1.0, 130, 3);
        timing::add(&timing::UNSHARP, t.elapsed());
    }

    let t = std::time::Instant::now();
    let threshold = otsu_threshold(&gray).clamp(0, 255);
    timing::add(&timing::OTSU, t.elapsed());
    let t = std::time::Instant::now();
    let mut pixels: Vec<u8> = gray
        .iter()
        .map(|&v| if v as i32 > threshold { 1 } else { 0 })
        .collect();
    let black = pixels.iter().filter(|&&p| p == 0).count();
    let white = pixels.len() - black;
    if black > white {
        for p in pixels.iter_mut() {
            *p ^= 1;
        }
    }
    timing::add(&timing::BINARIZE, t.elapsed());
    if opts.denoise {
        let t = std::time::Instant::now();
        despeckle(&mut pixels, wu, hu);
        timing::add(&timing::DESPECKLE, t.elapsed());
    }
    Binary {
        w,
        h,
        pixels,
        threshold,
    }
}

/// `engine.py::_despeckle`: drop black pixels that have fewer than
/// `min_neighbours` black 8-neighbours (out-of-bounds counts as not black), then
/// repeat with the 1-neighbour rule so pixels orphaned by the first pass go too.
/// A 1px line keeps two neighbours along its run, so thin lines survive.
///
/// Bit-parallel: each row is packed into 64-bit words, and "at least two of the
/// three neighbour rows have a black pixel here" is `(a & b) | (a & c) | (b & c)`,
/// so a whole word of pixels is decided at once.
pub fn despeckle(pixels: &mut [u8], w: usize, h: usize) {
    despeckle_pass(pixels, w, h, 2);
    despeckle_pass(pixels, w, h, 1);
}

fn despeckle_pass(pixels: &mut [u8], w: usize, h: usize, min_neighbours: u32) {
    use rayon::prelude::*;
    if w == 0 || h == 0 {
        return;
    }
    let words = (w + 63) / 64;
    // black[y * words + i] has a bit set where that pixel is black.
    let mut black = vec![0u64; words * h];
    for y in 0..h {
        let row = &pixels[y * w..(y + 1) * w];
        let out = &mut black[y * words..(y + 1) * words];
        for (x, value) in row.iter().enumerate() {
            if *value == 0 {
                out[x / 64] |= 1u64 << (x % 64);
            }
        }
    }

    // Neighbour masks of one word: bit x = "the pixel there is black".
    // `left`/`right` shift within the row, carrying across word boundaries.
    let spread = |row: &[u64], index: usize| -> (u64, u64, u64) {
        let word = row[index];
        let left_carry = if index > 0 { row[index - 1] >> 63 } else { 0 };
        let right_carry = if index + 1 < words {
            row[index + 1] << 63
        } else {
            0
        };
        (
            (word << 1) | left_carry,
            word,
            (word >> 1) | right_carry,
        )
    };

    let updated: Vec<Vec<u64>> = (0..h)
        .into_par_iter()
        .map(|y| {
            let same = &black[y * words..(y + 1) * words];
            let up = if y > 0 {
                Some(&black[(y - 1) * words..y * words])
            } else {
                None
            };
            let down = if y + 1 < h {
                Some(&black[(y + 1) * words..(y + 2) * words])
            } else {
                None
            };
            let mut row = vec![0u64; words];
            for index in 0..words {
                let (same_left, _, same_right) = spread(same, index);
                let (up_left, up_mid, up_right) = match up {
                    Some(up) => spread(up, index),
                    None => (0, 0, 0),
                };
                let (down_left, down_mid, down_right) = match down {
                    Some(down) => spread(down, index),
                    None => (0, 0, 0),
                };
                let masks = [
                    same_left, same_right, up_left, up_mid, up_right, down_left, down_mid,
                    down_right,
                ];
                // "At least two neighbours are black" = some pair of the eight
                // masks overlaps; that is exact for the two rules we use.
                let mut any = 0u64;
                let mut at_least_two = 0u64;
                for i in 0..masks.len() {
                    any |= masks[i];
                    for j in (i + 1)..masks.len() {
                        at_least_two |= masks[i] & masks[j];
                    }
                }
                let keep = if min_neighbours >= 2 {
                    at_least_two
                } else {
                    any
                };
                row[index] = same[index] & keep;
            }
            row
        })
        .collect();

    for y in 0..h {
        let row = &mut pixels[y * w..(y + 1) * w];
        for (x, value) in row.iter_mut().enumerate() {
            let is_black = (updated[y][x / 64] >> (x % 64)) & 1 == 1;
            if !is_black && *value == 0 {
                *value = 1;
            }
        }
    }
}

pub fn resize_gray(gray: &[u8], w: u32, h: u32, max_w: u32, max_h: u32) -> (Vec<u8>, u32, u32) {
    let started = std::time::Instant::now();
    // planes = 1: the pipeline only ever reads the R channel of the resized RGB,
    // so resizing the single channel produces byte-identical input to
    // `_prepare_image` while doing a third of the arithmetic.
    let result = resize_plane(gray, w, h, max_w, max_h, 1);
    timing::add(&timing::RESIZE, started.elapsed());
    result
}

/// Lanczos3 downscale, mirroring `Image.thumbnail(max_size, LANCZOS)`.
fn resize_plane(src: &[u8], w: u32, h: u32, max_w: u32, max_h: u32, planes: usize) -> (Vec<u8>, u32, u32) {
    if w <= max_w && h <= max_h {
        return (src.to_vec(), w, h);
    }
    let aspect = w as f64 / h as f64;
    let (tw, th) = if (max_w as f64 / max_h as f64) >= aspect {
        let y = max_h as f64;
        let x = round_aspect(y * aspect, |n| (aspect - n / y).abs());
        (x, max_h)
    } else {
        let x = max_w as f64;
        let y = round_aspect(x / aspect, |n| {
            if n == 0.0 { 0.0 } else { (aspect - x / n).abs() }
        });
        (max_w, y)
    };

    // Two steps, like Pillow's `thumbnail(..., reducing_gap=2.0)`: an integer box
    // reduction first (cheap, and it antialiases), then Lanczos3 on an image that
    // is only about twice the target. Running the kernel straight on a 9000px
    // wide strip is what made this the hot spot.
    let mut source = src.to_vec();
    let (mut sw, mut sh) = (w, h);
    let factor = ((w / tw).min(h / th)).max(1);
    if factor >= 2 {
        let started = std::time::Instant::now();
        let (reduced, rw, rh) = reduce_plane(&source, sw, sh, factor, planes);
        timing::add(&timing::RESIZE_REDUCE, started.elapsed());
        source = reduced;
        sw = rw;
        sh = rh;
    }
    if sw == tw && sh == th {
        return (source, tw, th);
    }
    (lanczos3_plane(&source, sw, sh, tw, th, planes), tw, th)
}

/// Pillow's `Image.reduce(factor)`: average factor x factor blocks (rounded).
fn reduce_plane(src: &[u8], w: u32, h: u32, factor: u32, planes: usize) -> (Vec<u8>, u32, u32) {
    use rayon::prelude::*;
    let (wu, hu, f) = (w as usize, h as usize, factor as usize);
    let (tw, th) = (wu / f, hu / f);
    let out_w = tw * planes;
    let area = (f * f) as u32;
    let mut out = vec![0u8; out_w * th];
    out.par_chunks_mut(out_w)
        .enumerate()
        .for_each(|(ty, out_row)| {
            for tx in 0..tw {
                let mut sums = [0u32; 3];
                for y in ty * f..(ty + 1) * f {
                    let base = y * wu * planes;
                    for x in tx * f..(tx + 1) * f {
                        let offset = base + x * planes;
                        for channel in 0..planes {
                            sums[channel] += src[offset + channel] as u32;
                        }
                    }
                }
                let base = tx * planes;
                for channel in 0..planes {
                    out_row[base + channel] = ((sums[channel] + area / 2) / area) as u8;
                }
            }
        });
    (out, tw as u32, th as u32)
}

/// Separable Lanczos3 with precomputed coefficients (no sin() in the inner loop),
/// parallel over rows.  Per-channel arithmetic is identical for every plane
/// count, so the single-channel pipeline keeps Pillow's sums bit-for-bit.
fn lanczos3_plane(src: &[u8], w: u32, h: u32, tw: u32, th: u32, planes: usize) -> Vec<u8> {
    use rayon::prelude::*;
    let (wu, hu, twu, thu) = (w as usize, h as usize, tw as usize, th as usize);
    let (h_starts, h_weights) = build_weights(wu, twu);
    let (v_starts, v_weights) = build_weights(hu, thu);

    let started_h = std::time::Instant::now();
    let mut horizontal = vec![0u8; twu * hu * planes];
    horizontal
        .par_chunks_mut(twu * planes)
        .enumerate()
        .for_each(|(y, out_row)| {
            let src_row = &src[y * wu * planes..(y + 1) * wu * planes];
            for tx in 0..twu {
                let start = h_starts[tx];
                let weights = &h_weights[tx];
                let mut sums = [0.0f64; 3];
                for (index, weight) in weights.iter().enumerate() {
                    let base = (start + index) * planes;
                    for channel in 0..planes {
                        sums[channel] += weight * src_row[base + channel] as f64;
                    }
                }
                let base = tx * planes;
                for channel in 0..planes {
                    out_row[base + channel] = sums[channel].round().clamp(0.0, 255.0) as u8;
                }
            }
        });
    timing::add(&timing::RESIZE_H, started_h.elapsed());

    let started_v = std::time::Instant::now();
    let mut out = vec![0u8; twu * thu * planes];
    out.par_chunks_mut(twu * planes)
        .enumerate()
        .for_each(|(ty, out_row)| {
            let start = v_starts[ty];
            let weights = &v_weights[ty];
            for tx in 0..twu {
                let mut sums = [0.0f64; 3];
                for (index, weight) in weights.iter().enumerate() {
                    let base = ((start + index) * twu + tx) * planes;
                    for channel in 0..planes {
                        sums[channel] += weight * horizontal[base + channel] as f64;
                    }
                }
                let base = tx * planes;
                for channel in 0..planes {
                    out_row[base + channel] = sums[channel].round().clamp(0.0, 255.0) as u8;
                }
            }
        });
    timing::add(&timing::RESIZE_V, started_v.elapsed());
    out
}

fn build_weights(in_len: usize, out_len: usize) -> (Vec<usize>, Vec<Vec<f64>>) {
    let scale = out_len as f64 / in_len as f64;
    let support = (3.0 / scale).max(1.0);
    let mut starts = Vec::with_capacity(out_len);
    let mut table = Vec::with_capacity(out_len);
    for index in 0..out_len {
        let center = (index as f64 + 0.5) / scale - 0.5;
        let start = (center - support).ceil().max(0.0) as usize;
        let end = ((center + support).floor() as i64)
            .min(in_len as i64 - 1)
            .max(start as i64) as usize;
        let mut weights = Vec::with_capacity(end - start + 1);
        let mut total = 0.0;
        for source in start..=end {
            let weight = lanczos((source as f64 - center) / support);
            weights.push(weight);
            total += weight;
        }
        if total != 0.0 {
            for weight in weights.iter_mut() {
                *weight /= total;
            }
        }
        starts.push(start);
        table.push(weights);
    }
    (starts, table)
}

/// Pillow's `Image.reduce(factor)`: average factor x factor blocks (rounded).


/// Separable Lanczos3 downscale, mirroring `Image.thumbnail(max_size, LANCZOS)`.
///
/// Bit-identical to a per-pixel formulation: every output pixel still sums its
/// taps in ascending source order, so the accumulated doubles are the same. What
/// changed is the traversal. The horizontal pass sweeps source columns and streams
/// weights into per-channel accumulator lanes (vector-friendly, one branch per
/// source pixel instead of per tap table lookup); the vertical pass walks taps as
/// whole-row vector loops. Source RGB is split into 3 planes up front so every
/// loop touches contiguous memory.


/// Flat per-output-index weight tables: `starts[i]` (first source index), `lens[i]`
/// taps starting at `offsets[i]` inside `weights`. Values are identical to the
/// nested form.


/// Per source column s, the contiguous destination window [k_lo(s), k_hi(s)] that
/// reads s. Destination windows are monotone: starts[] and ends[] both rise, so
/// the covering set is always contiguous. Sources covered by no window get an
/// empty (1..=0) range. Weight values are then read directly from the forward
/// table, keeping the accumulated doubles bit-identical.


fn lanczos(x: f64) -> f64 {
    if x.abs() < 1e-9 {
        return 1.0;
    }
    if x.abs() >= 3.0 {
        return 0.0;
    }
    let px = std::f64::consts::PI * x;
    let a = 3.0;
    (a * px.sin() * (px / a).sin()) / (px * px)
}

fn round_aspect(number: f64, key: impl Fn(f64) -> f64) -> u32 {
    let rounded = number.round();
    let candidates = [rounded - 1.0, rounded, rounded + 1.0];
    let mut best = rounded;
    let mut best_score = f64::MAX;
    for candidate in candidates {
        if candidate < 1.0 {
            continue;
        }
        let score = key(candidate);
        if score < best_score {
            best_score = score;
            best = candidate;
        }
    }
    best as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid(rows: &[&str]) -> (Vec<u8>, usize, usize) {
        let h = rows.len();
        let w = rows[0].len();
        let mut pixels = vec![1u8; w * h];
        for (y, row) in rows.iter().enumerate() {
            for (x, ch) in row.chars().enumerate() {
                if ch == '#' {
                    pixels[y * w + x] = 0;
                }
            }
        }
        (pixels, w, h)
    }

    #[test]
    fn despeckle_keeps_lines_and_drops_specks() {
        let (mut pixels, w, h) = grid(&[
            "#...........",
            "............",
            "..#.........",
            "..#.........",
            "............",
            ".....#####..",
            "............",
            ".....#####..",
            ".....#####..",
            "............",
        ]);
        despeckle(&mut pixels, w, h);
        let at = |x: usize, y: usize| pixels[y * w + x] == 0;
        assert!(!at(0, 0), "孤立点は消える");
        assert!(!at(2, 2) && !at(2, 3), "2画素のかたまりは消える");
        assert!((6..9).all(|x| at(x, 5)), "1画素幅の線の内部は残る");
        assert!(at(5, 5) == false && at(9, 5) == false, "1画素幅の線は両端1画素が削られる");
        assert!((6..10).all(|x| at(x, 7) && at(x, 8)), "2画素幅の線は残る");
    }

    #[test]
    fn despeckle_handles_word_boundaries() {
        // A dot at x=63 and a line crossing x=64 must not leak across words.
        // The dot sits two rows above the line so it really is isolated.
        let mut pixels = vec![1u8; 200 * 5];
        pixels[63] = 0; // isolated dot at the last bit of the first word
        for x in 60..=70 {
            pixels[3 * 200 + x] = 0; // 1px line crossing the word boundary
        }
        despeckle(&mut pixels, 200, 5);
        assert_eq!(pixels[63], 1, "孤立点は消える");
        assert!((61..=69).all(|x| pixels[3 * 200 + x] == 0), "語境界をまたぐ線は残る");
        assert_eq!(pixels[3 * 200 + 60], 1, "端は削られる");
        assert_eq!(pixels[3 * 200 + 70], 1, "端は削られる");
    }

    #[test]
    fn resize_gray_matches_rgb_r_channel() {
        // リサイズの単チャネル化は、RGBの3チャネル版とRチャネルがビット一致すること。
        // planes=3 と planes=1 に同じ R プレーンを流して比較する。
        let (mw, mh) = (40usize, 30usize);
        let mut rng_state = 123456789u64;
        let mut rand = move || {
            rng_state ^= rng_state << 13;
            rng_state ^= rng_state >> 7;
            rng_state ^= rng_state << 17;
            ((rng_state >> 24) & 0xff) as u8
        };
        // interleaved RGB where r = g = b = same plane (values don't matter for the check)
        let p: Vec<u8> = (0..mw * mh).map(|_| rand()).collect();
        let mut rgb = Vec::with_capacity(mw * mh * 3);
        for v in &p {
            rgb.push(*v);
            rgb.push(*v);
            rgb.push(*v);
        }
        for &(tw, th) in &[(33usize, 25usize), (40usize, 30usize), (20usize, 15usize)] {
            if tw >= mw && th >= mh {
                continue;
            }
            let (a, w1, h1) = resize_plane(&rgb, mw as u32, mh as u32, tw as u32, th as u32, 3);
            if tw == mw && th == mh {
                continue;
            }
            let (b, w2, h2) = resize_plane(&p, mw as u32, mh as u32, tw as u32, th as u32, 1);
            assert_eq!((w1, h1), (w2, h2));
            for ((ra, rb)) in a.chunks_exact(3).zip(b.iter()) {
                assert_eq!(ra[0], *rb, "Rチャネルの不一致 tw={} th={}", tw, th);
                assert_eq!(ra[1], *rb, "GチャネルはRと同じ値のはず");
                assert_eq!(ra[2], *rb, "BチャネルはRと同じ値のはず");
            }
        }
    }
}
