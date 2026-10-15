//! Decoding of the image filters a drawing PDF can contain.
//!
//! The Python engine relies on Pillow/pypdf for this; here the same filters are
//! decoded directly so the whole pipeline stays in one process.

use std::io::Read;

use lopdf::{Dictionary, Object};

/// A decoded raster, normalised to 8-bit RGB like Pillow's `convert("RGB")`.
pub struct Decoded {
    pub rgb: Vec<u8>,
    pub width: u32,
    pub height: u32,
    /// True when the source image is 1 bit per component (CCITT/G4 or 1-bit Flate).
    pub one_bit: bool,
}

pub fn filter_names(dict: &Dictionary) -> Vec<Vec<u8>> {
    match dict.get(b"Filter") {
        Ok(Object::Name(name)) => vec![name.clone()],
        Ok(Object::Array(items)) => items
            .iter()
            .filter_map(|item| match item {
                Object::Name(name) => Some(name.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

pub fn integer(dict: &Dictionary, key: &[u8]) -> Option<i64> {
    dict.get(key).ok().and_then(|value| match value {
        Object::Integer(number) => Some(*number),
        Object::Real(number) => Some(*number as i64),
        _ => None,
    })
}

pub fn is_one_bit_dict(dict: &Dictionary) -> bool {
    integer(dict, b"BitsPerComponent") == Some(1)
}

/// Decode one image XObject's raw stream bytes.
pub fn decode(dict: &Dictionary, data: &[u8]) -> Result<Decoded, String> {
    let filters = filter_names(dict);
    if filters.len() != 1 {
        return Err(format!(
            "unsupported filter chain {:?}",
            filters
                .iter()
                .map(|f| String::from_utf8_lossy(f).to_string())
                .collect::<Vec<_>>()
        ));
    }
    match filters[0].as_slice() {
        b"DCTDecode" => decode_jpeg(data),
        b"FlateDecode" => decode_flate(dict, data),
        b"CCITTFaxDecode" => decode_ccitt(dict, data),
        other => Err(format!(
            "unsupported filter {:?}",
            String::from_utf8_lossy(other)
        )),
    }
}

fn decode_jpeg(data: &[u8]) -> Result<Decoded, String> {
    let mut decoder = zune_jpeg::JpegDecoder::new(std::io::Cursor::new(data));
    let pixels = decoder
        .decode()
        .map_err(|e| format!("JPEG decode failed: {e}"))?;
    let info = decoder
        .info()
        .ok_or_else(|| "JPEG header not decoded".to_string())?;
    let (width, height) = (info.width as u32, info.height as u32);
    let count = (width * height) as usize;
    let rgb = match pixels.len() {
        // 1-component JPEG: replicate the single channel, like Pillow's convert("RGB")
        n if n == count => {
            let mut out = Vec::with_capacity(count * 3);
            for value in pixels {
                out.push(value);
                out.push(value);
                out.push(value);
            }
            out
        }
        n if n == count * 3 => pixels,
        n => {
            return Err(format!(
                "unsupported JPEG sample layout: {n} bytes for {width}x{height} (components={})",
                info.components
            ))
        }
    };
    Ok(Decoded {
        rgb,
        width,
        height,
        one_bit: false,
    })
}

fn decode_flate(dict: &Dictionary, data: &[u8]) -> Result<Decoded, String> {
    let mut inflater = flate2::read::ZlibDecoder::new(data);
    let mut raw = Vec::new();
    inflater
        .read_to_end(&mut raw)
        .map_err(|e| format!("FlateDecode failed: {e}"))?;

    let bits = integer(dict, b"BitsPerComponent").unwrap_or(8).clamp(1, 16) as u32;
    let colors = color_components(dict);
    let columns = integer(dict, b"Columns").unwrap_or(0) as usize;
    let rows = integer(dict, b"Rows").unwrap_or(0) as usize;

    if let Some(parms) = decode_parms(dict) {
        let predictor = integer(&parms, b"Predictor").unwrap_or(1);
        if predictor > 1 {
            let columns = if columns > 0 {
                columns
            } else {
                integer(dict, b"Width").unwrap_or(0) as usize
            };
            let colors = integer(&parms, b"Colors").unwrap_or(colors as i64) as usize;
            let parm_bits = integer(&parms, b"BitsPerComponent").unwrap_or(bits as i64) as u32;
            raw = undo_predictor(
                &raw,
                predictor,
                columns,
                colors,
                parm_bits,
                rows,
                integer(&parms, b"Columns").is_some(),
            )?;
        }
    }

    let width = integer(dict, b"Width").unwrap_or(0) as u32;
    let height = integer(dict, b"Height").unwrap_or(0) as u32;
    if width == 0 || height == 0 {
        return Err("FlateDecode image without dimensions".to_string());
    }
    let rgb = samples_to_rgb(&raw, width, height, bits, colors)?;
    Ok(Decoded {
        rgb,
        width,
        height,
        one_bit: bits == 1,
    })
}

fn decode_ccitt(dict: &Dictionary, data: &[u8]) -> Result<Decoded, String> {
    let width = integer(dict, b"Width").unwrap_or(0) as u32;
    let height = integer(dict, b"Height").unwrap_or(0) as u32;
    if width == 0 || height == 0 {
        return Err("CCITTFaxDecode image without dimensions".to_string());
    }
    let k = decode_parms(dict)
        .and_then(|parms| integer(&parms, b"K"))
        .unwrap_or(0);
    let mut rows: Vec<Vec<bool>> = Vec::with_capacity(height as usize);
    {
        let mut push_line = |line: &[u32]| {
            let mut row = Vec::with_capacity(width as usize);
            for color in fax::decoder::pels(line, width) {
                row.push(matches!(color, fax::Color::Black));
            }
            rows.push(row);
        };
        let decoded = if k < 0 {
            fax::decoder::decode_g4(data.iter().copied(), width, Some(height), &mut push_line)
                .is_some()
        } else {
            fax::decoder::decode_g3(data.iter().copied(), &mut push_line).is_some()
        };
        if !decoded {
            return Err("CCITTFaxDecode failed".to_string());
        }
    }
    let mut rgb = vec![255u8; (width * height) as usize * 3];
    for (y, row) in rows.iter().take(height as usize).enumerate() {
        for (x, black) in row.iter().take(width as usize).enumerate() {
            if *black {
                let offset = (y * width as usize + x) * 3;
                rgb[offset] = 0;
                rgb[offset + 1] = 0;
                rgb[offset + 2] = 0;
            }
        }
    }
    Ok(Decoded {
        rgb,
        width,
        height,
        one_bit: true,
    })
}

fn decode_parms(dict: &Dictionary) -> Option<Dictionary> {
    match dict.get(b"DecodeParms").or_else(|_| dict.get(b"DP")) {
        Ok(Object::Dictionary(parms)) => Some(parms.clone()),
        Ok(Object::Array(items)) => items.iter().find_map(|item| match item {
            Object::Dictionary(parms) => Some(parms.clone()),
            _ => None,
        }),
        _ => None,
    }
}

fn color_components(dict: &Dictionary) -> u32 {
    match dict.get(b"ColorSpace") {
        Ok(Object::Name(name)) => match name.as_slice() {
            b"DeviceGray" | b"G" | b"CalGray" => 1,
            b"DeviceCMYK" | b"CMYK" => 4,
            _ => 3,
        },
        _ => 3,
    }
}

/// Convert packed samples to 8-bit RGB the way Pillow's `convert("RGB")` would.
fn samples_to_rgb(
    raw: &[u8],
    width: u32,
    height: u32,
    bits: u32,
    colors: u32,
) -> Result<Vec<u8>, String> {
    let count = (width * height) as usize;
    let mut out = vec![0u8; count * 3];
    match (bits, colors) {
        (8, 1) => {
            for index in 0..count {
                let value = raw.get(index).copied().unwrap_or(255);
                out[index * 3] = value;
                out[index * 3 + 1] = value;
                out[index * 3 + 2] = value;
            }
        }
        (8, 3) => {
            let needed = count * 3;
            if raw.len() < needed {
                return Err(format!(
                    "FlateDecode produced {} bytes, expected {needed}",
                    raw.len()
                ));
            }
            out[..needed].copy_from_slice(&raw[..needed]);
        }
        (8, 4) => {
            for index in 0..count {
                let base = index * 4;
                let (c, m, y, k) = (
                    raw.get(base).copied().unwrap_or(255) as u32,
                    raw.get(base + 1).copied().unwrap_or(255) as u32,
                    raw.get(base + 2).copied().unwrap_or(255) as u32,
                    raw.get(base + 3).copied().unwrap_or(255) as u32,
                );
                // Pillow converts CMYK to RGB with the naive 255 - min(255, c + k) rule.
                out[index * 3] = (255 - (c + k).min(255)) as u8;
                out[index * 3 + 1] = (255 - (m + k).min(255)) as u8;
                out[index * 3 + 2] = (255 - (y + k).min(255)) as u8;
            }
        }
        (1, _) => {
            let row_bytes = (width as usize + 7) / 8;
            for y in 0..height as usize {
                for x in 0..width as usize {
                    let byte = raw.get(y * row_bytes + x / 8).copied().unwrap_or(255);
                    let bit = (byte >> (7 - (x % 8))) & 1;
                    let value = if bit == 1 { 255 } else { 0 };
                    let offset = (y * width as usize + x) * 3;
                    out[offset] = value;
                    out[offset + 1] = value;
                    out[offset + 2] = value;
                }
            }
        }
        (b, c) => return Err(format!("unsupported FlateDecode layout: {b} bits x {c} colours")),
    }
    Ok(out)
}

/// Undo the TIFF/PNG predictors used by FlateDecode image data.
fn undo_predictor(
    raw: &[u8],
    predictor: i64,
    columns: usize,
    colors: usize,
    bits: u32,
    _rows: usize,
    columns_from_parms: bool,
) -> Result<Vec<u8>, String> {
    if columns == 0 || colors == 0 {
        return Ok(raw.to_vec());
    }
    let row_bytes = (columns * colors * bits as usize + 7) / 8;
    let bpp = ((colors * bits as usize) + 7) / 8;
    let mut out = Vec::with_capacity(raw.len());
    if predictor == 2 {
        let mut previous = vec![0u8; row_bytes];
        for chunk in raw.chunks(row_bytes) {
            let mut row = chunk.to_vec();
            row.resize(row_bytes, 0);
            for index in bpp..row_bytes {
                row[index] = row[index].wrapping_add(row[index - bpp]);
            }
            for index in 0..row_bytes {
                row[index] = row[index].wrapping_add(previous[index]);
            }
            previous = row.clone();
            out.extend_from_slice(&row);
        }
        return Ok(out);
    }
    if !(10..=15).contains(&predictor) {
        return Err(format!("unsupported Predictor {predictor}"));
    }
    let _ = columns_from_parms;
    let mut previous = vec![0u8; row_bytes];
    let mut index = 0usize;
    while index + 1 + row_bytes <= raw.len() {
        let kind = raw[index];
        let mut row = raw[index + 1..index + 1 + row_bytes].to_vec();
        for x in 0..row_bytes {
            let left = if x >= bpp { row[x - bpp] } else { 0 };
            let up = previous[x];
            let up_left = if x >= bpp { previous[x - bpp] } else { 0 };
            row[x] = match kind {
                0 => row[x],
                1 => row[x].wrapping_add(left),
                2 => row[x].wrapping_add(up),
                3 => row[x].wrapping_add(((left as u16 + up as u16) / 2) as u8),
                4 => {
                    let p = left as i16 + up as i16 - up_left as i16;
                    let (pa, pb, pc) = (
                        (p - left as i16).abs(),
                        (p - up as i16).abs(),
                        (p - up_left as i16).abs(),
                    );
                    let pred = if pa <= pb && pa <= pc {
                        left
                    } else if pb <= pc {
                        up
                    } else {
                        up_left
                    };
                    row[x].wrapping_add(pred)
                }
                other => return Err(format!("unsupported PNG predictor filter {other}")),
            };
        }
        previous = row.clone();
        out.extend_from_slice(&row);
        index += 1 + row_bytes;
    }
    Ok(out)
}

/// Composite an image over white using its soft mask, like pypdf does before the
/// Python engine's own white compositing.
pub fn composite_over_white(rgb: &mut [u8], mask: &Decoded) {
    if mask.width == 0 || mask.height == 0 {
        return;
    }
    let (mw, mh) = (mask.width as usize, mask.height as usize);
    let (iw, ih) = (rgb.len() / 3, 0);
    let _ = ih;
    let height = iw / (mw.max(1));
    for index in 0..iw {
        let (x, y) = (index % mw, index / mw);
        if x >= mw || y >= height {
            break;
        }
        let alpha = mask.rgb.get(index * 3).copied().unwrap_or(255) as u32;
        for channel in 0..3 {
            let value = rgb[index * 3 + channel] as u32;
            // Pillow's alpha_composite onto white: v*a/255 + 255*(1 - a/255)
            rgb[index * 3 + channel] = ((value * alpha + 255 * (255 - alpha)) / 255) as u8;
        }
    }
    let _ = mh;
}
