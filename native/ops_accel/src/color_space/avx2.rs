use super::LUT_SIZE;
use crate::simd::x86::{clamp, load_rgb, luma, select, store_rgb};
use std::arch::x86_64::*;

#[target_feature(enable = "avx2")]
pub(super) unsafe fn transfer(buffer: &mut [f32], lut: &[f32; LUT_SIZE], inverse: bool) {
    let end = buffer.len() / 8 * 8;
    let zero = _mm256_setzero_ps();
    let one = _mm256_set1_ps(1.0);
    let last = _mm256_set1_epi32((LUT_SIZE - 1) as i32);
    for offset in (0..end).step_by(8) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let value = _mm256_loadu_ps(ptr);
        let safe = select(
            _mm256_cmp_ps::<_CMP_UNORD_Q>(value, value),
            zero,
            clamp(value, 0.0, 1.0),
        );
        let index = _mm256_mul_ps(safe, _mm256_set1_ps((LUT_SIZE - 1) as f32));
        let i0 = _mm256_cvttps_epi32(index);
        let i1 = _mm256_min_epi32(_mm256_add_epi32(i0, _mm256_set1_epi32(1)), last);
        let t = _mm256_sub_ps(
            _mm256_mul_ps(value, _mm256_set1_ps((LUT_SIZE - 1) as f32)),
            _mm256_cvtepi32_ps(i0),
        );
        let a = _mm256_i32gather_ps::<4>(lut.as_ptr(), i0);
        let b = _mm256_i32gather_ps::<4>(lut.as_ptr(), i1);
        let output = _mm256_add_ps(_mm256_mul_ps(a, _mm256_sub_ps(one, t)), _mm256_mul_ps(b, t));
        let output = select(_mm256_cmp_ps::<_CMP_LE_OQ>(value, zero), zero, output);
        if inverse {
            _mm256_storeu_ps(ptr, output);
            let high = _mm256_movemask_ps(_mm256_cmp_ps::<_CMP_GE_OQ>(value, one));
            if high != 0 {
                let mut values = [0.0f32; 8];
                _mm256_storeu_ps(values.as_mut_ptr(), value);
                for lane in 0..8 {
                    if high & (1 << lane) != 0 {
                        *ptr.add(lane) = ((values[lane] + 0.055) / 1.055).powf(2.4);
                    }
                }
            }
        } else {
            let output = select(_mm256_cmp_ps::<_CMP_GE_OQ>(value, one), one, output);
            _mm256_storeu_ps(ptr, clamp(output, 0.0, 1.0));
        }
    }
    for value in &mut buffer[end..] {
        *value = if inverse {
            super::srgb_to_linear_val(*value, lut)
        } else {
            super::linear_to_srgb_val(*value, lut).clamp(0.0, 1.0)
        };
    }
}

#[target_feature(enable = "avx2")]
pub(super) unsafe fn gamut(buffer: &mut [f32]) {
    let end = buffer.len() / 24 * 24;
    let one = _mm256_set1_ps(1.0);
    for offset in (0..end).step_by(24) {
        let ptr = buffer.as_mut_ptr().add(offset);
        let [r, g, b] = load_rgb(ptr);
        let lum = luma(r, g, b);
        let chroma = [r, g, b].map(|v| _mm256_sub_ps(v, lum));
        let mut scale = one;
        for c in chroma {
            let positive = _mm256_cmp_ps::<_CMP_GT_OQ>(c, _mm256_set1_ps(1e-8));
            let negative = _mm256_cmp_ps::<_CMP_LT_OQ>(c, _mm256_set1_ps(-1e-8));
            let limit = select(
                positive,
                _mm256_div_ps(_mm256_sub_ps(one, lum), c),
                select(
                    negative,
                    _mm256_div_ps(_mm256_xor_ps(lum, _mm256_set1_ps(-0.0)), c),
                    scale,
                ),
            );
            scale = select(_mm256_cmp_ps::<_CMP_LT_OQ>(limit, scale), limit, scale);
        }
        scale = clamp(scale, 0.0, 1.0);
        store_rgb(
            ptr,
            chroma.map(|c| _mm256_add_ps(lum, _mm256_mul_ps(c, scale))),
        );
    }
    super::gamut_compress_portable(&mut buffer[end..]);
}
