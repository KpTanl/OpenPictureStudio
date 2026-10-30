use rayon::prelude::*;
use crate::simd::{self, Operator};

pub fn raw_xyz_to_linear_srgb(source: &[u16], maxval: f32, matrix: [[f64; 3]; 3],
                            width: usize, out: &mut [f32]) {
    if width == 0 || out.is_empty() {
        return;
    }
    let vector = simd::enabled(Operator::Raw);
    out.par_chunks_mut(width * 3 * 16)
        .zip(source.par_chunks(width * 3 * 16))
        .for_each(|(target, input)| {
            #[cfg(target_arch = "x86_64")]
            if vector {
                return unsafe { convert_avx2(input, maxval, matrix, target) };
            }
            convert_portable(input, maxval, matrix, target);
        });
}

fn convert_portable(input: &[u16], maxval: f32, matrix: [[f64; 3]; 3], target: &mut [f32]) {
    for (output, pixel) in target.chunks_exact_mut(3).zip(input.chunks_exact(3)) {
        let x = (pixel[0] as f32 / maxval) as f64;
        let y = (pixel[1] as f32 / maxval) as f64;
        let z = (pixel[2] as f32 / maxval) as f64;
        for channel in 0..3 {
            output[channel] = (x * matrix[channel][0] + y * matrix[channel][1]
                + z * matrix[channel][2]) as f32;
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn convert_avx2(input: &[u16], maxval: f32, matrix: [[f64; 3]; 3], target: &mut [f32]) {
    use std::arch::x86_64::*;
    use crate::simd::x86::{unpack_rgb, store_rgb};
    let end = target.len() / 24 * 24;
    let denominator = _mm256_set1_ps(maxval);
    for offset in (0..end).step_by(24) {
        let normalized = std::array::from_fn::<_, 3, _>(|block| {
            let values = _mm_loadu_si128(input.as_ptr().add(offset + block * 8).cast());
            _mm256_div_ps(_mm256_cvtepi32_ps(_mm256_cvtepu16_epi32(values)), denominator)
        });
        let rgb = unpack_rgb(normalized[0], normalized[1], normalized[2]);
        let low = rgb.map(|v| _mm256_cvtps_pd(_mm256_castps256_ps128(v)));
        let high = rgb.map(|v| _mm256_cvtps_pd(_mm256_extractf128_ps::<1>(v)));
        let result = std::array::from_fn(|channel| {
            let coefficients = matrix[channel].map(|v| _mm256_set1_pd(v));
            let convert = |v: [__m256d; 3]| _mm256_cvtpd_ps(_mm256_add_pd(
                _mm256_add_pd(_mm256_mul_pd(v[0], coefficients[0]), _mm256_mul_pd(v[1], coefficients[1])),
                _mm256_mul_pd(v[2], coefficients[2])));
            _mm256_insertf128_ps::<1>(_mm256_castps128_ps256(convert(low)), convert(high))
        });
        store_rgb(target.as_mut_ptr().add(offset), result);
    }
    convert_portable(&input[end..], maxval, matrix, &mut target[end..]);
}
