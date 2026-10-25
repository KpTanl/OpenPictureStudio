use rayon::prelude::*;
use std::sync::OnceLock;

const CELLS: usize = 4096;

/// 线性值到 8 位 sRGB 的量化表。thresholds[k] 是输出取到 k 的最小线性值，
/// 即 EOTF((k - 0.5) / 255)；base[c] 是线性值 c / CELLS 对应的输出。
struct DisplayTable {
    thresholds: [f32; 257],
    base: [u8; CELLS],
}

static TABLE: OnceLock<DisplayTable> = OnceLock::new();

fn table() -> &'static DisplayTable {
    TABLE.get_or_init(|| {
        let mut thresholds = [f32::INFINITY; 257];
        thresholds[0] = f32::NEG_INFINITY;
        for level in 1..256 {
            let encoded = (level as f64 - 0.5) / 255.0;
            let linear = if encoded <= 0.04045 {
                encoded / 12.92
            } else {
                ((encoded + 0.055) / 1.055).powf(2.4)
            };
            thresholds[level] = linear as f32;
        }
        let mut base = [0u8; CELLS];
        let mut level = 0usize;
        for (cell, slot) in base.iter_mut().enumerate() {
            let edge = cell as f32 / CELLS as f32;
            while edge >= thresholds[level + 1] {
                level += 1;
            }
            *slot = level as u8;
        }
        DisplayTable { thresholds, base }
    })
}

#[inline(always)]
fn quantize(table: &DisplayTable, value: f32) -> u8 {
    if !(value > 0.0) {
        return 0;
    }
    if value >= 1.0 {
        return 255;
    }
    let mut level = table.base[(value * CELLS as f32) as usize] as usize;
    while value >= table.thresholds[level + 1] {
        level += 1;
    }
    level as u8
}

/// 线性 RGB（HxWx3）与可选直通 Alpha（HxW）转为显示用 RGBA8（HxWx4）。
/// 语义同 OETF → 裁剪到 [0, 1] → round(x * 255)；无 Alpha 时填 255。
pub fn linear_to_display_rgba8(pixels: &[f32], alpha: Option<&[f32]>, width: usize, out: &mut [u8]) {
    if width == 0 || out.is_empty() {
        return;
    }
    let table = table();
    out.par_chunks_exact_mut(width * 4)
        .zip(pixels.par_chunks_exact(width * 3))
        .enumerate()
        .for_each(|(row, (target, source))| {
            let alpha_row = alpha.map(|values| &values[row * width..(row + 1) * width]);
            for column in 0..width {
                let pixel = &source[column * 3..column * 3 + 3];
                let output = &mut target[column * 4..column * 4 + 4];
                output[0] = quantize(table, pixel[0]);
                output[1] = quantize(table, pixel[1]);
                output[2] = quantize(table, pixel[2]);
                output[3] = match alpha_row {
                    Some(values) => (values[column].clamp(0.0, 1.0) * 255.0).round_ties_even() as u8,
                    None => 255,
                };
            }
        });
}
