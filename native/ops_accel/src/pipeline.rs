use rayon::prelude::*;
use crate::color_space::{
    linear_to_srgb_buffer, srgb_to_linear_buffer, gamut_compress_buffer,
    get_lin_to_srgb_lut, get_srgb_to_lin_lut,
};
use crate::color_mixer::{apply_color_mixer, MixerSettings};
use crate::tone::{
    white_balance, exposure, contrast, vibrance, saturation,
    apply_curves, apply_look_stage, local_tone, detail_effects, LookStage,
    white_balance_gains, exposure_scale, exposure_scale_v2, exposure_v2, contrast_exponent,
};

pub struct RenderParams {
    pub temperature: i32,
    pub tint: i32,
    pub exposure: f32,
    pub process_version: u8,
    pub contrast: f32,
    pub highlights: i32,
    pub shadows: i32,
    pub whites: i32,
    pub blacks: i32,
    pub texture: i32,
    pub clarity: i32,
    pub dehaze: i32,
    pub vibrance: i32,
    pub saturation: f32,
    pub look_stages: Vec<LookStage>,
    pub mixer: MixerSettings,
    pub rgb_lut: Option<Vec<f32>>,
    pub r_lut: Option<Vec<f32>>,
    pub g_lut: Option<Vec<f32>>,
    pub b_lut: Option<Vec<f32>>,
}

pub fn render_pipeline(
    input: &[f32],
    buffer: &mut [f32],
    width: usize,
    height: usize,
    params: &RenderParams,
) {
    render_pipeline_with_mixer_input(input, buffer, width, height, params, None);
}

pub fn render_pipeline_with_mixer_input(
    input: &[f32],
    buffer: &mut [f32],
    width: usize,
    height: usize,
    params: &RenderParams,
    mut mixer_input: Option<&mut [f32]>,
) {
    let gains = white_balance_gains(params.temperature, params.tint);
    let scale = if params.process_version == 2 {
        exposure_scale_v2(params.exposure)
    } else {
        exposure_scale(params.exposure)
    };
    let exponent = contrast_exponent(params.contrast);
    let oetf = get_lin_to_srgb_lut();
    let eotf = get_srgb_to_lin_lut();
    let band_size = width.max(1) * 3 * 16;
    let has_local = params.shadows != 0 || params.highlights != 0
        || params.blacks != 0 || params.whites != 0;
    let has_detail = params.texture != 0 || params.clarity != 0 || params.dehaze != 0;

    let middle = |band: &mut [f32], sample: Option<&mut [f32]>| {
        if let Some(a) = exponent {
            contrast(band, a);
        }
        apply_curves(band, params.rgb_lut.as_deref(), params.r_lut.as_deref(),
                     params.g_lut.as_deref(), params.b_lut.as_deref());
        if let Some(sample) = sample {
            sample.copy_from_slice(band);
        }
        apply_color_mixer(band, &params.mixer);
        vibrance(band, params.vibrance);
        saturation(band, params.saturation);
    };
    let finish = |band: &mut [f32]| {
        gamut_compress_buffer(band);
        srgb_to_linear_buffer(band, eotf);
    };

    let initialize = |band: &mut [f32], source: &[f32], sample: Option<&mut [f32]>| {
            band.copy_from_slice(source);
            if let Some(gains) = gains {
                white_balance(band, gains);
            }
            if let Some(scale) = scale {
                if params.process_version == 2 {
                    exposure_v2(band, scale);
                } else {
                    exposure(band, scale);
                }
            }
            linear_to_srgb_buffer(band, oetf);
            for stage in &params.look_stages {
                apply_look_stage(band, stage);
            }
            if !has_local {
                middle(band, sample);
                if !has_detail {
                    finish(band);
                }
            }
    };
    if let Some(sample) = mixer_input.as_deref_mut() {
        buffer.par_chunks_mut(band_size).zip(input.par_chunks(band_size))
            .zip(sample.par_chunks_mut(band_size))
            .for_each(|((band, source), captured)| initialize(band, source, Some(captured)));
    } else {
        buffer.par_chunks_mut(band_size).zip(input.par_chunks(band_size))
            .for_each(|(band, source)| initialize(band, source, None));
    }

    if has_local {
        local_tone(buffer, width, height, params.shadows, params.highlights,
                   params.blacks, params.whites);
        let apply_middle = |band: &mut [f32], sample: Option<&mut [f32]>| {
            middle(band, sample);
            if !has_detail {
                finish(band);
            }
        };
        if let Some(sample) = mixer_input.as_deref_mut() {
            buffer.par_chunks_mut(band_size).zip(sample.par_chunks_mut(band_size))
                .for_each(|(band, captured)| apply_middle(band, Some(captured)));
        } else {
            buffer.par_chunks_mut(band_size).for_each(|band| apply_middle(band, None));
        }
    }
    if has_detail {
        detail_effects(buffer, width, height, params.texture, params.clarity, params.dehaze);
        buffer.par_chunks_mut(band_size).for_each(finish);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::color_mixer::ChannelAdjust;

    #[test]
    fn test_pipeline_step_by_step() {
        let mut buf = vec![1.0f32, 0.0f32, 0.0f32];
        let p = RenderParams {
            temperature: 0,
            tint: 0,
            exposure: 0.0,
            process_version: 1,
            contrast: 1.0,
            highlights: 0,
            shadows: 0,
            whites: 0,
            blacks: 0,
            texture: 0,
            clarity: 0,
            dehaze: 0,
            vibrance: 0,
            saturation: 1.0,
            look_stages: Vec::new(),
            mixer: MixerSettings {
                channels: vec![ChannelAdjust {
                    center: 0.0,
                    width: 35.0,
                    hue: 25.0,
                    sat: -30.0,
                    lum: 15.0,
                }],
            },
            rgb_lut: None,
            r_lut: None,
            g_lut: None,
            b_lut: None,
        };
        let input = buf.clone();
        render_pipeline(&input, &mut buf, 1, 1, &p);
        let lut = crate::color_space::get_lin_to_srgb_lut();
        let srgb_r = crate::color_space::linear_to_srgb_val(buf[0], lut);
        let srgb_g = crate::color_space::linear_to_srgb_val(buf[1], lut);
        let srgb_b = crate::color_space::linear_to_srgb_val(buf[2], lut);
        assert!((srgb_r - 0.8725).abs() < 1e-3, "srgb_r={}", srgb_r);
        assert!((srgb_g - 0.38906).abs() < 1e-3, "srgb_g={}", srgb_g);
        assert!((srgb_b - 0.2775).abs() < 1e-3, "srgb_b={}", srgb_b);
    }
}
