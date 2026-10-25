#[derive(Clone, Debug)]
pub struct ChannelAdjust {
    pub center: f32,
    pub width: f32,
    pub hue: f32,
    pub sat: f32,
    pub lum: f32,
}

#[derive(Clone, Debug)]
pub struct MixerSettings {
    pub channels: Vec<ChannelAdjust>,
}

impl MixerSettings {
    pub fn is_empty(&self) -> bool {
        self.channels.is_empty()
    }
}

#[inline]
pub fn rgb_to_hsl(r: f32, g: f32, b: f32) -> (f32, f32, f32) {
    let cmax = r.max(g).max(b);
    let cmin = r.min(g).min(b);
    let chroma = cmax - cmin;
    let l = (cmax + cmin) * 0.5;

    if chroma < 1e-7 {
        return (0.0, 0.0, l);
    }

    let mut h = if (cmax - r).abs() < 1e-7 {
        60.0 * (((g - b) / chroma) % 6.0)
    } else if (cmax - g).abs() < 1e-7 {
        60.0 * (((b - r) / chroma) + 2.0)
    } else {
        60.0 * (((r - g) / chroma) + 4.0)
    };

    h %= 360.0;
    if h < 0.0 {
        h += 360.0;
    }

    let denom = 1.0 - (2.0 * l - 1.0).abs();
    let s = if denom < 1e-7 {
        0.0
    } else {
        (chroma / denom).clamp(0.0, 1.0)
    };

    (h, s, l)
}

#[inline]
pub fn hsl_to_rgb(h: f32, s: f32, l: f32) -> (f32, f32, f32) {
    let c = (1.0 - (2.0 * l - 1.0).abs()) * s;
    let h_prime = (h / 60.0) % 6.0;
    let x = c * (1.0 - ((h_prime % 2.0) - 1.0).abs());
    let m = l - c * 0.5;

    let sector = (h_prime.floor() as i32) % 6;
    let (r1, g1, b1) = match sector {
        0 => (c, x, 0.0),
        1 => (x, c, 0.0),
        2 => (0.0, c, x),
        3 => (0.0, x, c),
        4 => (x, 0.0, c),
        _ => (c, 0.0, x),
    };

    (
        (r1 + m).clamp(0.0, 1.0),
        (g1 + m).clamp(0.0, 1.0),
        (b1 + m).clamp(0.0, 1.0),
    )
}

pub fn apply_color_mixer(buffer: &mut [f32], settings: &MixerSettings) {
    if settings.is_empty() {
        return;
    }

    let channels = &settings.channels;
    let num_channels = channels.len();

    buffer.chunks_exact_mut(3).for_each(|pix| {
        let r = pix[0];
        let g = pix[1];
        let b = pix[2];

        let (h, s, l) = rgb_to_hsl(r, g, b);
        if s < 1e-6 {
            return;
        }

        // 低饱和软死区
        let c0: f32 = 0.02;
        let c1: f32 = 0.08;
        let t_c = ((s - c0) / (c1 - c0)).clamp(0.0, 1.0);
        let s_mask = t_c * t_c * (3.0 - 2.0 * t_c);

        if s_mask <= 1e-6 {
            return;
        }

        // 收集各通道原始权重并统计总权重
        let mut raw_weights = [0.0f32; 8];
        let mut total_raw_weight = 0.0f32;

        for i in 0..num_channels {
            let ch = &channels[i];
            let mut diff = (h - ch.center + 180.0) % 360.0;
            if diff < 0.0 { diff += 360.0; }
            diff = (diff - 180.0).abs();

            if diff < ch.width {
                let cos_val = (std::f32::consts::PI * 0.5 * (diff / ch.width)).cos();
                let w = cos_val * cos_val * s_mask;
                raw_weights[i] = w;
                total_raw_weight += w;
            }
        }

        if total_raw_weight <= 1e-6 {
            return;
        }

        let norm_factor = 1.0 / total_raw_weight.max(1.0);

        let mut delta_h = 0.0f32;
        let mut delta_s = 0.0f32;
        let mut delta_l = 0.0f32;

        for i in 0..num_channels {
            let w = raw_weights[i] * norm_factor;
            if w <= 1e-7 {
                continue;
            }
            let ch = &channels[i];

            if ch.hue != 0.0 {
                delta_h += w * (ch.hue / 100.0 * 45.0);
            }
            if ch.sat > 0.0 {
                delta_s += w * (ch.sat / 100.0) * (1.0 - s);
            } else if ch.sat < 0.0 {
                delta_s += w * (ch.sat / 100.0) * s;
            }
            if ch.lum > 0.0 {
                delta_l += w * (ch.lum / 100.0) * (1.0 - l);
            } else if ch.lum < 0.0 {
                delta_l += w * (ch.lum / 100.0) * l;
            }
        }

        let mut new_h = (h + delta_h) % 360.0;
        if new_h < 0.0 {
            new_h += 360.0;
        }
        let new_s = (s + delta_s).clamp(0.0, 1.0);
        let new_l = (l + delta_l).clamp(0.0, 1.0);

        let (out_r, out_g, out_b) = hsl_to_rgb(new_h, new_s, new_l);
        pix[0] = out_r;
        pix[1] = out_g;
        pix[2] = out_b;
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_color_mixer_red_pixel() {
        let mut buf = vec![1.0f32, 0.0f32, 0.0f32];
        let settings = MixerSettings {
            channels: vec![ChannelAdjust {
                center: 0.0,
                width: 35.0,
                hue: 25.0,
                sat: -30.0,
                lum: 15.0,
            }],
        };
        apply_color_mixer(&mut buf, &settings);
        println!("Rust apply_color_mixer out: {:?}", buf);
        assert!((buf[0] - 0.8725).abs() < 1e-3, "buf[0]={}", buf[0]);
        assert!((buf[1] - 0.38906).abs() < 1e-3, "buf[1]={}", buf[1]);
        assert!((buf[2] - 0.2775).abs() < 1e-3, "buf[2]={}", buf[2]);
    }
}
