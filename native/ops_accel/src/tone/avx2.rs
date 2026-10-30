use crate::simd::x86::{clamp, load_rgb, luma, max, min, select, store_rgb};
use std::arch::x86_64::*;

#[target_feature(enable = "avx2")]
pub(super) unsafe fn white_balance(buffer: &mut [f32], gains: [f32; 3]) {
    let vector_gains = gains.map(|v| _mm256_set1_ps(v));
    let end = buffer.len() / 24 * 24;
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let rgb = load_rgb(ptr);
        store_rgb(
            ptr,
            std::array::from_fn(|c| _mm256_mul_ps(rgb[c], vector_gains[c])),
        );
    }
    super::white_balance_portable(&mut buffer[end..], gains);
}

#[inline]
#[target_feature(enable = "avx2")]
unsafe fn shoulder(value: __m256, scale: __m256, coefficient: __m256) -> __m256 {
    let knee = _mm256_set1_ps(0.75);
    let one = _mm256_set1_ps(1.0);
    let y = _mm256_mul_ps(value, scale);
    let d = _mm256_sub_ps(y, knee);
    let compressed = min(
        _mm256_add_ps(
            knee,
            _mm256_div_ps(d, _mm256_add_ps(one, _mm256_mul_ps(d, coefficient))),
        ),
        one,
    );
    select(_mm256_cmp_ps::<_CMP_LE_OQ>(y, knee), y, compressed)
}

#[target_feature(enable = "avx2")]
pub(super) unsafe fn exposure_v2(buffer: &mut [f32], scale: f32) {
    let scale_v = _mm256_set1_ps(scale);
    if scale <= 1.0 {
        let end = buffer.len() / 8 * 8;
        for offset in (0..end).step_by(8) {
            let ptr = buffer.as_mut_ptr().add(offset);
            _mm256_storeu_ps(ptr, _mm256_mul_ps(_mm256_loadu_ps(ptr), scale_v));
        }
        super::exposure_v2_portable(&mut buffer[end..], scale);
        return;
    }
    let white = scale.sqrt();
    let coefficient = _mm256_set1_ps((white - 1.0) / (0.25 * (white - 0.75)));
    let end = buffer.len() / 24 * 24;
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let rgb = load_rgb(ptr).map(|v| clamp(v, 0.0, 1.0));
        let hi = max(max(rgb[0], rgb[1]), rgb[2]);
        let lo = min(min(rgb[0], rgb[1]), rgb[2]);
        let span = _mm256_sub_ps(hi, lo);
        let high_out = shoulder(hi, scale_v, coefficient);
        let low_out = shoulder(lo, scale_v, coefficient);
        let out = rgb.map(|v| {
            let ratio = select(
                _mm256_cmp_ps::<_CMP_GT_OQ>(span, _mm256_setzero_ps()),
                _mm256_div_ps(_mm256_sub_ps(v, lo), span),
                _mm256_setzero_ps(),
            );
            _mm256_add_ps(
                low_out,
                _mm256_mul_ps(_mm256_sub_ps(high_out, low_out), ratio),
            )
        });
        store_rgb(ptr, out);
    }
    super::exposure_v2_portable(&mut buffer[end..], scale);
}

#[inline]
#[target_feature(enable = "avx2")]
unsafe fn smooth(chroma: __m256) -> (__m256, __m256) {
    let t = clamp(
        _mm256_div_ps(
            _mm256_sub_ps(chroma, _mm256_set1_ps(0.03)),
            _mm256_set1_ps(0.12 - 0.03),
        ),
        0.0,
        1.0,
    );
    let curve = _mm256_sub_ps(_mm256_set1_ps(3.0), _mm256_mul_ps(_mm256_set1_ps(2.0), t));
    (t, curve)
}

#[inline]
#[target_feature(enable = "avx2")]
unsafe fn apply_gain(rgb: [__m256; 3], lum: __m256, gain: __m256) -> [__m256; 3] {
    rgb.map(|v| _mm256_add_ps(lum, _mm256_mul_ps(_mm256_sub_ps(v, lum), gain)))
}

#[target_feature(enable = "avx2")]
pub(super) unsafe fn vibrance(buffer: &mut [f32], amount: i32) {
    let strength = amount as f32 / 100.0;
    let one = _mm256_set1_ps(1.0);
    let end = buffer.len() / 24 * 24;
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let [r, g, b] = load_rgb(ptr);
        let lum = luma(r, g, b);
        let hi = select(_mm256_cmp_ps::<_CMP_GT_OQ>(g, r), g, r);
        let hi = select(_mm256_cmp_ps::<_CMP_GT_OQ>(b, hi), b, hi);
        let lo = select(_mm256_cmp_ps::<_CMP_LT_OQ>(g, r), g, r);
        let lo = select(_mm256_cmp_ps::<_CMP_LT_OQ>(b, lo), b, lo);
        let chroma = clamp(_mm256_sub_ps(hi, lo), 0.0, 1.0);
        let gain = if strength > 0.0 {
            let (t, curve) = smooth(chroma);
            let w = _mm256_mul_ps(_mm256_mul_ps(t, t), curve);
            _mm256_add_ps(
                one,
                _mm256_mul_ps(
                    _mm256_mul_ps(_mm256_set1_ps(strength * 0.6), w),
                    _mm256_sub_ps(one, chroma),
                ),
            )
        } else {
            _mm256_set1_ps(1.0 + strength * 0.75)
        };
        store_rgb(ptr, apply_gain([r, g, b], lum, gain));
    }
    super::vibrance_portable(&mut buffer[end..], amount);
}

#[target_feature(enable = "avx2")]
pub(super) unsafe fn saturation(buffer: &mut [f32], factor: f32) {
    let factor_v = _mm256_set1_ps(factor);
    let end = buffer.len() / 24 * 24;
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let rgb = load_rgb(ptr);
        let lum = luma(rgb[0], rgb[1], rgb[2]);
        store_rgb(
            ptr,
            rgb.map(|v| _mm256_add_ps(lum, _mm256_mul_ps(factor_v, _mm256_sub_ps(v, lum)))),
        );
    }
    super::saturation_portable(&mut buffer[end..], factor);
}

#[target_feature(enable = "avx2")]
pub(super) unsafe fn local_saturation(buffer: &mut [f32], vibrance: &[f32], saturation: &[f32]) {
    let end = buffer.len() / 24 * 24;
    let one = _mm256_set1_ps(1.0);
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let [r, g, b] = load_rgb(ptr);
        let lum = luma(r, g, b);
        let chroma = clamp(
            _mm256_sub_ps(max(max(r, g), b), min(min(r, g), b)),
            0.0,
            1.0,
        );
        let amount = _mm256_div_ps(
            _mm256_loadu_ps(vibrance.as_ptr().add(offset / 3)),
            _mm256_set1_ps(100.0),
        );
        let (t, curve) = smooth(chroma);
        let gain = _mm256_mul_ps(amount, _mm256_set1_ps(0.6));
        let gain = _mm256_mul_ps(_mm256_mul_ps(gain, t), t);
        let gain = _mm256_add_ps(
            one,
            _mm256_mul_ps(_mm256_mul_ps(gain, curve), _mm256_sub_ps(one, chroma)),
        );
        let negative = _mm256_add_ps(one, _mm256_mul_ps(amount, _mm256_set1_ps(0.75)));
        let gain = select(
            _mm256_cmp_ps::<_CMP_GT_OQ>(amount, _mm256_setzero_ps()),
            gain,
            negative,
        );
        let rgb = apply_gain([r, g, b], lum, gain);
        let lum = luma(rgb[0], rgb[1], rgb[2]);
        let factor = _mm256_loadu_ps(saturation.as_ptr().add(offset / 3));
        store_rgb(ptr, apply_gain(rgb, lum, factor));
    }
    super::local_saturation_portable(
        &mut buffer[end..],
        &vibrance[end / 3..],
        &saturation[end / 3..],
    );
}

#[inline]
#[target_feature(enable = "avx2")]
unsafe fn sample(lut: &[f32], value: __m256) -> __m256 {
    if lut.is_empty() {
        return value;
    }
    if lut.len() == 1 {
        return _mm256_set1_ps(lut[0]);
    }
    let index = _mm256_mul_ps(
        clamp(value, 0.0, 1.0),
        _mm256_set1_ps((lut.len() - 1) as f32),
    );
    let address = select(
        _mm256_cmp_ps::<_CMP_UNORD_Q>(index, index),
        _mm256_setzero_ps(),
        index,
    );
    let last = _mm256_set1_epi32((lut.len() - 1) as i32);
    let i0 = _mm256_min_epi32(_mm256_cvttps_epi32(address), last);
    let i1 = _mm256_min_epi32(_mm256_add_epi32(i0, _mm256_set1_epi32(1)), last);
    let t = _mm256_sub_ps(index, _mm256_cvtepi32_ps(i0));
    let a = _mm256_i32gather_ps::<4>(lut.as_ptr(), i0);
    let b = _mm256_i32gather_ps::<4>(lut.as_ptr(), i1);
    _mm256_add_ps(
        _mm256_mul_ps(a, _mm256_sub_ps(_mm256_set1_ps(1.0), t)),
        _mm256_mul_ps(b, t),
    )
}

#[target_feature(enable = "avx2")]
pub(super) unsafe fn curves(buffer: &mut [f32], luts: [Option<&[f32]>; 4]) {
    let end = buffer.len() / 24 * 24;
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let mut rgb = load_rgb(ptr);
        if let Some(lut) = luts[0] {
            rgb = rgb.map(|v| sample(lut, v));
        }
        for c in 0..3 {
            if let Some(lut) = luts[c + 1] {
                rgb[c] = sample(lut, rgb[c]);
            }
        }
        store_rgb(ptr, rgb);
    }
    super::curves_portable(&mut buffer[end..], luts[0], luts[1], luts[2], luts[3]);
}
