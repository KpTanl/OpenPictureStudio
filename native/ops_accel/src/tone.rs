use rayon::prelude::*;
use crate::color_space::{calc_rec709_luma, calc_hue_deg, REC709_R, REC709_G, REC709_B};
use crate::guided_filter::fast_guided_filter;

const ZONE_PEAK: f32 = 45.0 / 255.0;
const GAIN_KNEE: f32 = 16.0 / 255.0;

pub fn white_balance_gains(temp: i32, tint: i32) -> Option<[f32; 3]> {
    if temp == 0 && tint == 0 {
        return None;
    }
    let t_ev = (temp.clamp(-100, 100) as f32) / 100.0 * 1.35;
    let tint_ev = (tint.clamp(-100, 100) as f32) / 100.0 * 0.80;

    let kr = (2.0f32).powf(t_ev);
    let kg = (2.0f32).powf(-tint_ev);
    let kb = (2.0f32).powf(-t_ev);

    let lum_scale = (REC709_R * kr + REC709_G * kg + REC709_B * kb).max(1e-4);
    let gain_r = kr / lum_scale;
    let gain_g = kg / lum_scale;
    let gain_b = kb / lum_scale;

    Some([gain_r, gain_g, gain_b])
}

pub fn white_balance(buffer: &mut [f32], gains: [f32; 3]) {
    buffer.chunks_exact_mut(3).for_each(|pix| {
        pix[0] *= gains[0];
        pix[1] *= gains[1];
        pix[2] *= gains[2];
    });
}

pub fn exposure_scale(ev: f32) -> Option<f32> {
    if ev.abs() < 1e-6 {
        return None;
    }
    let ev_clamped = ev.clamp(-10.0, 10.0);
    Some((2.0f32).powf(ev_clamped))
}

pub fn exposure(buffer: &mut [f32], scale: f32) {
    let knee = 0.75f32;
    let s = 0.7f32;

    buffer.iter_mut().for_each(|v| {
        let y = *v * scale;
        if y > knee {
            *v = knee + s * (1.0 - (-(y - knee) / s).exp());
        } else {
            *v = y;
        }
    });
}

pub fn exposure_scale_v2(ev: f32) -> Option<f32> {
    if ev == 0.0 {
        return None;
    }
    Some(2.0f64.powf(ev.clamp(-10.0, 10.0) as f64) as f32)
}

pub fn exposure_v2(buffer: &mut [f32], scale: f32) {
    if scale <= 1.0 {
        buffer.iter_mut().for_each(|value| *value *= scale);
        return;
    }
    let knee = 0.75f32;
    let white = scale.sqrt();
    let coefficient = (white - 1.0) / ((1.0 - knee) * (white - knee));
    let shoulder = |value: f32| {
        let y = value * scale;
        if y <= knee {
            y
        } else {
            (knee + (y - knee) / (1.0 + (y - knee) * coefficient)).min(1.0)
        }
    };
    buffer.chunks_exact_mut(3).for_each(|pixel| {
        let rgb = [pixel[0].clamp(0.0, 1.0), pixel[1].clamp(0.0, 1.0), pixel[2].clamp(0.0, 1.0)];
        let high = rgb[0].max(rgb[1]).max(rgb[2]);
        let low = rgb[0].min(rgb[1]).min(rgb[2]);
        let span = high - low;
        let high_out = shoulder(high);
        let low_out = shoulder(low);
        for channel in 0..3 {
            let ratio = if span > 0.0 { (rgb[channel] - low) / span } else { 0.0 };
            pixel[channel] = low_out + (high_out - low_out) * ratio;
        }
    });
}

pub fn contrast_exponent(factor: f32) -> Option<f32> {
    if (factor - 1.0).abs() < 1e-4 {
        return None;
    }
    Some(factor.max(1e-3))
}

pub fn contrast(buffer: &mut [f32], a: f32) {
    buffer.iter_mut().for_each(|v| {
        let d = v.clamp(0.0, 1.0);
        let da = d.powf(a);
        let ia = (1.0 - d).powf(a);
        *v = da / (da + ia + 1e-12);
    });
}

pub fn vibrance(buffer: &mut [f32], amount: i32) {
    if amount == 0 {
        return;
    }
    let strength = amount as f32 / 100.0;
    let s0: f32 = 0.03;
    let s1: f32 = 0.12;

    buffer.chunks_exact_mut(3).for_each(|pix| {
        let r = pix[0];
        let g = pix[1];
        let b = pix[2];
        let lum = calc_rec709_luma(r, g, b);

        let mut mx = r;
        let mut mn = r;
        if g > mx { mx = g; }
        if b > mx { mx = b; }
        if g < mn { mn = g; }
        if b < mn { mn = b; }

        let s = (mx - mn).clamp(0.0, 1.0);

        let gain = if strength > 0.0 {
            let t = ((s - s0) / (s1 - s0)).clamp(0.0, 1.0);
            let w_lo = t * t * (3.0 - 2.0 * t);
            1.0 + (strength * 0.6) * w_lo * (1.0 - s)
        } else {
            1.0 + strength * 0.75
        };

        pix[0] = lum + (r - lum) * gain;
        pix[1] = lum + (g - lum) * gain;
        pix[2] = lum + (b - lum) * gain;
    });
}

pub fn saturation(buffer: &mut [f32], factor: f32) {
    if (factor - 1.0).abs() < 1e-4 {
        return;
    }
    buffer.chunks_exact_mut(3).for_each(|pix| {
        let r = pix[0];
        let g = pix[1];
        let b = pix[2];
        let lum = calc_rec709_luma(r, g, b);

        pix[0] = lum + factor * (r - lum);
        pix[1] = lum + factor * (g - lum);
        pix[2] = lum + factor * (b - lum);
    });
}

#[inline(always)]
fn sample_lut_1d(lut: &[f32], val: f32) -> f32 {
    let n = lut.len();
    if n == 0 { return val; }
    if n == 1 { return lut[0]; }

    let idx = val.clamp(0.0, 1.0) * (n - 1) as f32;
    let i0 = idx.floor() as usize;
    let i1 = (i0 + 1).min(n - 1);
    let t = idx - i0 as f32;
    lut[i0] * (1.0 - t) + lut[i1] * t
}

pub fn apply_curves(
    buffer: &mut [f32],
    rgb_lut: Option<&[f32]>,
    r_lut: Option<&[f32]>,
    g_lut: Option<&[f32]>,
    b_lut: Option<&[f32]>,
) {
    if rgb_lut.is_none() && r_lut.is_none() && g_lut.is_none() && b_lut.is_none() {
        return;
    }

    buffer.chunks_exact_mut(3).for_each(|pix| {
        let mut r = pix[0];
        let mut g = pix[1];
        let mut b = pix[2];

        if let Some(lut) = rgb_lut {
            r = sample_lut_1d(lut, r);
            g = sample_lut_1d(lut, g);
            b = sample_lut_1d(lut, b);
        }

        if let Some(lut) = r_lut { r = sample_lut_1d(lut, r); }
        if let Some(lut) = g_lut { g = sample_lut_1d(lut, g); }
        if let Some(lut) = b_lut { b = sample_lut_1d(lut, b); }

        pix[0] = r;
        pix[1] = g;
        pix[2] = b;
    });
}

/// 一层 OPS 观感，曲线与饱和表由 Python 侧 core/looks.py 传入（33 点 / 12 段）。
pub struct LookStage {
    pub tone_lut: Vec<f32>,
    pub hue_sat: Vec<f32>,
    pub tone_strength: f32,
    pub sat_strength: f32,
}

pub fn apply_look_stage(buffer: &mut [f32], stage: &LookStage) {
    let s_tone = stage.tone_strength.clamp(0.0, 1.0);
    let s_sat = stage.sat_strength.clamp(0.0, 1.0);
    let bins = stage.hue_sat.len();
    buffer.chunks_exact_mut(3).for_each(|pix| {
        let (r, g, b) = (pix[0], pix[1], pix[2]);

        // 1. apply_tone：等增益保色相
        let (mut tr, mut tg, mut tb) = (r, g, b);
        if s_tone > 0.0 {
            let lum = calc_rec709_luma(r, g, b);
            let lp = sample_lut_1d(&stage.tone_lut, lum);
            let gain = lp / lum.max(1e-4);
            tr = (r * gain).clamp(0.0, 1.0);
            tg = (g * gain).clamp(0.0, 1.0);
            tb = (b * gain).clamp(0.0, 1.0);
            if s_tone < 1.0 {
                tr = (1.0 - s_tone) * r + s_tone * tr;
                tg = (1.0 - s_tone) * g + s_tone * tg;
                tb = (1.0 - s_tone) * b + s_tone * tb;
            }
        }

        // 2. apply_hue_sat：按色相缩放 chroma
        if s_sat > 0.0 {
            let h_deg = calc_hue_deg(tr, tg, tb);
            let binw = 360.0 / bins as f32;
            let mut h_adj = h_deg - binw * 0.5;
            if h_adj < 0.0 { h_adj += 360.0; }
            let idx = h_adj / binw;
            let i0 = (idx as usize).min(bins - 1);
            let i1 = (i0 + 1) % bins;
            let t = idx - i0 as f32;
            let hg = stage.hue_sat[i0] * (1.0 - t) + stage.hue_sat[i1] * t;

            let lt = calc_rec709_luma(tr, tg, tb);
            let mut sr = (lt + hg * (tr - lt)).clamp(0.0, 1.0);
            let mut sg = (lt + hg * (tg - lt)).clamp(0.0, 1.0);
            let mut sb = (lt + hg * (tb - lt)).clamp(0.0, 1.0);
            if s_sat < 1.0 {
                sr = (1.0 - s_sat) * tr + s_sat * sr;
                sg = (1.0 - s_sat) * tg + s_sat * sg;
                sb = (1.0 - s_sat) * tb + s_sat * sb;
            }
            tr = sr;
            tg = sg;
            tb = sb;
        }
        pix[0] = tr;
        pix[1] = tg;
        pix[2] = tb;
    });
}

#[inline(always)]
fn calc_zone_offset(
    l: f32,
    z0: f32,
    z1: f32,
    amount: i32,
    point_control: bool,
    bright_anchored: bool,
) -> f32 {
    if amount == 0 {
        return 0.0;
    }
    let span = z1 - z0;
    if span <= 1e-6 {
        return 0.0;
    }
    let u = ((l - z0) / span).clamp(0.0, 1.0);
    let d = if bright_anchored { 1.0 - u } else { u };
    let mut w = if point_control {
        1.0 - d
    } else {
        (std::f32::consts::PI * d).sin()
    };
    if l < z0 || l > z1 {
        w = 0.0;
    } else {
        w = w.clamp(0.0, 1.0);
    }
    let smooth_w = w * w * (3.0 - 2.0 * w);
    (amount as f32 / 100.0) * smooth_w * ZONE_PEAK
}

fn apply_equal_gain(buffer: &mut [f32], l_orig: &[f32], l_target: &[f32]) {
    buffer
        .par_chunks_exact_mut(3)
        .zip(l_orig.par_iter())
        .zip(l_target.par_iter())
        .for_each(|((pix, &l), &lp)| {
            let eps = 1e-3 / 255.0;
            let gain = lp / l.max(eps);
            let delta = lp - l;
            let blend = (l / GAIN_KNEE).clamp(0.0, 1.0);

            for c in pix.iter_mut() {
                let multiplied = *c * gain;
                let added = *c + delta;
                *c = blend * multiplied + (1.0 - blend) * added;
            }
        });
}

pub fn local_tone(
    buffer: &mut [f32],
    width: usize,
    height: usize,
    sh: i32,
    hl: i32,
    bk: i32,
    wh: i32,
) {
    if sh == 0 && hl == 0 && bk == 0 && wh == 0 {
        return;
    }

    let len = width * height;
    let mut l_orig = vec![0.0f32; len];
    l_orig
        .par_iter_mut()
        .zip(buffer.par_chunks_exact(3))
        .for_each(|(l, pix)| {
            *l = calc_rec709_luma(pix[0], pix[1], pix[2]);
        });

    let minhw = width.min(height);

    let l_r = if sh != 0 || hl != 0 {
        let r = 4.max(minhw / 48);
        let s = (minhw as f32 / 256.0).clamp(2.0, 6.0) as usize;
        let base = fast_guided_filter(&l_orig, &l_orig, width, height, r, 0.04 * 0.04, s);

        let z_mid = 128.0 / 255.0;
        let mut lr = vec![0.0f32; len];

        lr.par_iter_mut()
            .zip(l_orig.par_iter())
            .zip(base.par_iter())
            .for_each(|((out_lr, &orig), &b)| {
                let mut mod_b = b;
                if sh != 0 {
                    mod_b += calc_zone_offset(b, 0.0, z_mid, sh, false, false);
                }
                if hl != 0 {
                    mod_b += calc_zone_offset(b, z_mid, 1.0, hl, false, true);
                }
                let detail = orig - b;
                *out_lr = mod_b + detail;
            });
        lr
    } else {
        l_orig.clone()
    };

    let mut l_target = l_r;
    if bk != 0 || wh != 0 {
        l_target.par_iter_mut().for_each(|lp| {
            let cur = *lp;
            let mut off = 0.0f32;
            if bk != 0 {
                off += calc_zone_offset(cur, 0.0, 80.0 / 255.0, bk, true, false);
            }
            if wh != 0 {
                off += calc_zone_offset(cur, 175.0 / 255.0, 1.0, wh, true, true);
            }
            *lp = (cur + off).clamp(0.0, 1.0);
        });
    } else {
        l_target.par_iter_mut().for_each(|lp| {
            *lp = (*lp).clamp(0.0, 1.0);
        });
    }

    apply_equal_gain(buffer, &l_orig, &l_target);
}

pub fn detail_effects(
    buffer: &mut [f32],
    width: usize,
    height: usize,
    tx: i32,
    cl: i32,
    dz: i32,
) {
    if tx == 0 && cl == 0 && dz == 0 {
        return;
    }

    let len = width * height;
    let mut l_orig = vec![0.0f32; len];
    l_orig
        .par_iter_mut()
        .zip(buffer.par_chunks_exact(3))
        .for_each(|(l, pix)| {
            *l = calc_rec709_luma(pix[0], pix[1], pix[2]);
        });

    let minhw = width.min(height);
    let mut lp = l_orig.clone();

    if tx != 0 || cl != 0 {
        let r_f = 2.max(minhw / 256);
        let s_f = (minhw as f32 / 512.0).clamp(1.0, 4.0) as usize;
        let base_fine = fast_guided_filter(&l_orig, &l_orig, width, height, r_f, 0.10 * 0.10, s_f);

        if tx != 0 {
            let tx_scale = (tx as f32) / 100.0;
            lp.par_iter_mut()
                .zip(l_orig.par_iter())
                .zip(base_fine.par_iter())
                .for_each(|((p, &orig), &bf)| {
                    *p += tx_scale * (orig - bf);
                });
        }

        if cl != 0 {
            let r_m = 4.max(minhw / 24);
            let s_m = (minhw as f32 / 200.0).clamp(2.0, 6.0) as usize;
            let base_mid = fast_guided_filter(&l_orig, &l_orig, width, height, r_m, 0.14 * 0.14, s_m);
            let cl_scale = 0.8 * (cl as f32) / 100.0;

            lp.par_iter_mut()
                .zip(base_fine.par_iter())
                .zip(base_mid.par_iter())
                .for_each(|((p, &bf), &bm)| {
                    *p += cl_scale * (bf - bm);
                });
        }
    }

    if dz != 0 {
        let r_c = 8.max(minhw / 8);
        let s_c = (minhw as f32 / 128.0).clamp(4.0, 12.0) as usize;
        let base_coarse = fast_guided_filter(&l_orig, &l_orig, width, height, r_c, 0.20 * 0.20, s_c);
        let dz_scale = 0.5 * (dz as f32) / 100.0;

        lp.par_iter_mut()
            .zip(l_orig.par_iter())
            .zip(base_coarse.par_iter())
            .for_each(|((p, &orig), &bc)| {
                *p += dz_scale * (orig - bc);
            });
    }

    lp.par_iter_mut().for_each(|p| {
        *p = (*p).clamp(0.0, 1.0);
    });

    apply_equal_gain(buffer, &l_orig, &lp);

    if dz > 0 {
        let sat_boost = 1.0 + 0.10 * (dz as f32) / 100.0;
        buffer.par_chunks_exact_mut(3).for_each(|pix| {
            let r = pix[0];
            let g = pix[1];
            let b = pix[2];
            let lum = calc_rec709_luma(r, g, b);
            pix[0] = lum + sat_boost * (r - lum);
            pix[1] = lum + sat_boost * (g - lum);
            pix[2] = lum + sat_boost * (b - lum);
        });
    }
}
