use std::sync::OnceLock;

pub const REC709_R: f32 = 0.2126;
pub const REC709_G: f32 = 0.7152;
pub const REC709_B: f32 = 0.0722;

const SRGB_EOTF_THRESHOLD: f32 = 0.04045;
const SRGB_OETF_THRESHOLD: f32 = 0.0031308;

const LUT_SIZE: usize = 4096;

static SRGB_TO_LIN_LUT: OnceLock<[f32; LUT_SIZE]> = OnceLock::new();
static LIN_TO_SRGB_LUT: OnceLock<[f32; LUT_SIZE]> = OnceLock::new();

pub fn get_srgb_to_lin_lut() -> &'static [f32; LUT_SIZE] {
    SRGB_TO_LIN_LUT.get_or_init(|| {
        let mut lut = [0.0f32; LUT_SIZE];
        for i in 0..LUT_SIZE {
            let c = (i as f32) / (LUT_SIZE - 1) as f32;
            lut[i] = if c <= SRGB_EOTF_THRESHOLD {
                c / 12.92
            } else {
                ((c + 0.055) / 1.055).powf(2.4)
            };
        }
        lut
    })
}

pub fn get_lin_to_srgb_lut() -> &'static [f32; LUT_SIZE] {
    LIN_TO_SRGB_LUT.get_or_init(|| {
        let mut lut = [0.0f32; LUT_SIZE];
        for i in 0..LUT_SIZE {
            let c = (i as f32) / (LUT_SIZE - 1) as f32;
            lut[i] = if c <= SRGB_OETF_THRESHOLD {
                c * 12.92
            } else {
                1.055 * c.powf(1.0 / 2.4) - 0.055
            };
        }
        lut
    })
}

#[inline(always)]
pub fn srgb_to_linear_val(c: f32, lut: &[f32; LUT_SIZE]) -> f32 {
    if c <= 0.0 {
        return 0.0;
    }
    if c >= 1.0 {
        return ((c + 0.055) / 1.055).powf(2.4);
    }
    let idx_f = c * (LUT_SIZE - 1) as f32;
    let i0 = idx_f as usize;
    let i1 = (i0 + 1).min(LUT_SIZE - 1);
    let t = idx_f - i0 as f32;
    lut[i0] * (1.0 - t) + lut[i1] * t
}

#[inline(always)]
pub fn linear_to_srgb_val(c: f32, lut: &[f32; LUT_SIZE]) -> f32 {
    if c <= 0.0 {
        return 0.0;
    }
    if c >= 1.0 {
        return 1.0;
    }
    let idx_f = c * (LUT_SIZE - 1) as f32;
    let i0 = idx_f as usize;
    let i1 = (i0 + 1).min(LUT_SIZE - 1);
    let t = idx_f - i0 as f32;
    lut[i0] * (1.0 - t) + lut[i1] * t
}

#[inline(always)]
pub fn calc_rec709_luma(r: f32, g: f32, b: f32) -> f32 {
    r * REC709_R + g * REC709_G + b * REC709_B
}

#[inline(always)]
pub fn calc_hue_deg(r: f32, g: f32, b: f32) -> f32 {
    let mut mx = r;
    let mut mn = r;
    if g > mx { mx = g; }
    if b > mx { mx = b; }
    if g < mn { mn = g; }
    if b < mn { mn = b; }

    let diff = mx - mn;
    if diff <= 1e-8 {
        return 0.0;
    }

    let h = if (mx - r).abs() < 1e-12 {
        let val = 60.0 * ((g - b) / diff) + 360.0;
        if val >= 360.0 { val - 360.0 } else { val }
    } else if (mx - g).abs() < 1e-12 {
        60.0 * ((b - r) / diff) + 120.0
    } else {
        60.0 * ((r - g) / diff) + 240.0
    };
    h
}

pub fn srgb_to_linear_buffer(buffer: &mut [f32], lut: &[f32; LUT_SIZE]) {
    buffer.iter_mut().for_each(|v| {
        *v = srgb_to_linear_val(*v, lut);
    });
}

pub fn linear_to_srgb_buffer(buffer: &mut [f32], lut: &[f32; LUT_SIZE]) {
    buffer.iter_mut().for_each(|v| {
        *v = linear_to_srgb_val(*v, lut).clamp(0.0, 1.0);
    });
}

pub fn gamut_compress_buffer(buffer: &mut [f32]) {
    buffer.chunks_exact_mut(3).for_each(|pix| {
        let r = pix[0];
        let g = pix[1];
        let b = pix[2];
        let lum = calc_rec709_luma(r, g, b);

        let cr = r - lum;
        let cg = g - lum;
        let cb = b - lum;

        let eps = 1e-8;
        let mut max_s: f32 = 1.0;

        for &c in &[cr, cg, cb] {
            if c > eps {
                let limit = (1.0 - lum) / c;
                if limit < max_s { max_s = limit; }
            } else if c < -eps {
                let limit = -lum / c;
                if limit < max_s { max_s = limit; }
            }
        }

        max_s = max_s.clamp(0.0, 1.0);
        pix[0] = lum + cr * max_s;
        pix[1] = lum + cg * max_s;
        pix[2] = lum + cb * max_s;
    });
}
