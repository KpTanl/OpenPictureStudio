use crate::simd::{self, Operator};
use rayon::prelude::*;

const CHUNK: usize = 4096;

pub(crate) fn products(left: &[f32], right: &[f32]) -> Vec<f32> {
    let mut out = vec![0.0; left.len()];
    out.par_chunks_mut(CHUNK)
        .enumerate()
        .for_each(|(chunk, target)| {
            let start = chunk * CHUNK;
            product(
                &left[start..start + target.len()],
                &right[start..start + target.len()],
                target,
            );
        });
    out
}

pub(crate) fn coefficient_fields(
    mg: &[f32],
    ms: &[f32],
    cg: &[f32],
    cgs: &[f32],
    eps: f32,
) -> (Vec<f32>, Vec<f32>) {
    let mut a = vec![0.0; mg.len()];
    let mut b = vec![0.0; mg.len()];
    a.par_chunks_mut(CHUNK)
        .zip(b.par_chunks_mut(CHUNK))
        .enumerate()
        .for_each(|(chunk, (a, b))| {
            let start = chunk * CHUNK;
            let end = start + a.len();
            coefficients(
                &mg[start..end],
                &ms[start..end],
                &cg[start..end],
                &cgs[start..end],
                eps,
                a,
                b,
            );
        });
    (a, b)
}

pub(crate) fn combine(guide: &[f32], a: &[f32], b: &[f32]) -> Vec<f32> {
    let mut out = vec![0.0; guide.len()];
    out.par_chunks_mut(CHUNK)
        .enumerate()
        .for_each(|(chunk, target)| {
            let start = chunk * CHUNK;
            let end = start + target.len();
            output(&guide[start..end], &a[start..end], &b[start..end], target);
        });
    out
}

pub(crate) fn product(left: &[f32], right: &[f32], out: &mut [f32]) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) {
        return unsafe { product_avx2(left, right, out) };
    }
    for i in 0..out.len() {
        out[i] = left[i] * right[i];
    }
}

pub(crate) fn coefficients(
    mg: &[f32],
    ms: &[f32],
    cg: &[f32],
    cgs: &[f32],
    eps: f32,
    a: &mut [f32],
    b: &mut [f32],
) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) {
        return unsafe { coefficients_avx2(mg, ms, cg, cgs, eps, a, b) };
    }
    for i in 0..a.len() {
        let var = cg[i] - mg[i] * mg[i];
        let cov = cgs[i] - mg[i] * ms[i];
        a[i] = cov / (var + eps);
        b[i] = ms[i] - a[i] * mg[i];
    }
}

pub(crate) fn output(guide: &[f32], a: &[f32], b: &[f32], out: &mut [f32]) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) {
        return unsafe { output_avx2(guide, a, b, out) };
    }
    for i in 0..out.len() {
        out[i] = a[i] * guide[i] + b[i];
    }
}

pub(crate) fn accumulate(sums: &mut [f64], row: &[f32], subtract: bool) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) {
        return unsafe { accumulate_avx2(sums, row, subtract) };
    }
    if subtract {
        for i in 0..sums.len() {
            sums[i] -= row[i] as f64;
        }
    } else {
        for i in 0..sums.len() {
            sums[i] += row[i] as f64;
        }
    }
}

pub(crate) fn divide(sums: &[f64], area: f64, out: &mut [f32]) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) {
        return unsafe { divide_avx2(sums, area, out) };
    }
    for i in 0..out.len() {
        out[i] = (sums[i] / area) as f32;
    }
}

pub(crate) fn integral_row(previous: &[f64], row: &[f32], out: &mut [f64]) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) {
        return unsafe { integral_row_avx2(previous, row, out) };
    }
    for i in 0..out.len() {
        out[i] = previous[i] + row[i] as f64;
    }
}

pub(crate) fn resize_row(top: &[f32], bottom: &[f32], scale: f32, wy: f32, out: &mut [f32]) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Filter) && top.len() <= 1 << 24 {
        return unsafe { resize_row_avx2(top, bottom, scale, wy, out) };
    }
    resize_row_portable(top, bottom, scale, wy, out, 0);
}

fn resize_row_portable(
    top: &[f32],
    bottom: &[f32],
    scale: f32,
    wy: f32,
    out: &mut [f32],
    start: usize,
) {
    for (offset, value) in out.iter_mut().enumerate() {
        let xs = ((start + offset) as f32 + 0.5) * scale - 0.5;
        let xs = xs.clamp(0.0, top.len() as f32 - 1.0);
        let x0 = xs.floor() as usize;
        let x1 = (x0 + 1).min(top.len() - 1);
        let wx = xs - x0 as f32;
        let a = top[x0] * (1.0 - wx) + top[x1] * wx;
        let b = bottom[x0] * (1.0 - wx) + bottom[x1] * wx;
        *value = a * (1.0 - wy) + b * wy;
    }
}

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn product_avx2(left: &[f32], right: &[f32], out: &mut [f32]) {
    let end = out.len() / 8 * 8;
    for i in (0..end).step_by(8) {
        _mm256_storeu_ps(
            out.as_mut_ptr().add(i),
            _mm256_mul_ps(
                _mm256_loadu_ps(left.as_ptr().add(i)),
                _mm256_loadu_ps(right.as_ptr().add(i)),
            ),
        );
    }
    for i in end..out.len() {
        out[i] = left[i] * right[i];
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn coefficients_avx2(
    mg: &[f32],
    ms: &[f32],
    cg: &[f32],
    cgs: &[f32],
    eps: f32,
    a: &mut [f32],
    b: &mut [f32],
) {
    let end = a.len() / 8 * 8;
    for i in (0..end).step_by(8) {
        let mg = _mm256_loadu_ps(mg.as_ptr().add(i));
        let ms = _mm256_loadu_ps(ms.as_ptr().add(i));
        let cg = _mm256_loadu_ps(cg.as_ptr().add(i));
        let cgs = _mm256_loadu_ps(cgs.as_ptr().add(i));
        let var = _mm256_sub_ps(cg, _mm256_mul_ps(mg, mg));
        let cov = _mm256_sub_ps(cgs, _mm256_mul_ps(mg, ms));
        let aa = _mm256_div_ps(cov, _mm256_add_ps(var, _mm256_set1_ps(eps)));
        let bb = _mm256_sub_ps(ms, _mm256_mul_ps(aa, mg));
        _mm256_storeu_ps(a.as_mut_ptr().add(i), aa);
        _mm256_storeu_ps(b.as_mut_ptr().add(i), bb);
    }
    for i in end..a.len() {
        let var = cg[i] - mg[i] * mg[i];
        let cov = cgs[i] - mg[i] * ms[i];
        a[i] = cov / (var + eps);
        b[i] = ms[i] - a[i] * mg[i];
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn output_avx2(guide: &[f32], a: &[f32], b: &[f32], out: &mut [f32]) {
    let end = out.len() / 8 * 8;
    for i in (0..end).step_by(8) {
        let value = _mm256_add_ps(
            _mm256_mul_ps(
                _mm256_loadu_ps(a.as_ptr().add(i)),
                _mm256_loadu_ps(guide.as_ptr().add(i)),
            ),
            _mm256_loadu_ps(b.as_ptr().add(i)),
        );
        _mm256_storeu_ps(out.as_mut_ptr().add(i), value);
    }
    for i in end..out.len() {
        out[i] = a[i] * guide[i] + b[i];
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn accumulate_avx2(sums: &mut [f64], row: &[f32], subtract: bool) {
    let end = sums.len() / 4 * 4;
    for i in (0..end).step_by(4) {
        let ptr = sums.as_mut_ptr().add(i);
        let a = _mm256_loadu_pd(ptr);
        let b = _mm256_cvtps_pd(_mm_loadu_ps(row.as_ptr().add(i)));
        let value = if subtract {
            _mm256_sub_pd(a, b)
        } else {
            _mm256_add_pd(a, b)
        };
        _mm256_storeu_pd(ptr, value);
    }
    for i in end..sums.len() {
        if subtract {
            sums[i] -= row[i] as f64;
        } else {
            sums[i] += row[i] as f64;
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn divide_avx2(sums: &[f64], area: f64, out: &mut [f32]) {
    let end = out.len() / 4 * 4;
    for i in (0..end).step_by(4) {
        _mm_storeu_ps(
            out.as_mut_ptr().add(i),
            _mm256_cvtpd_ps(_mm256_div_pd(
                _mm256_loadu_pd(sums.as_ptr().add(i)),
                _mm256_set1_pd(area),
            )),
        );
    }
    for i in end..out.len() {
        out[i] = (sums[i] / area) as f32;
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn integral_row_avx2(previous: &[f64], row: &[f32], out: &mut [f64]) {
    let end = out.len() / 4 * 4;
    for i in (0..end).step_by(4) {
        _mm256_storeu_pd(
            out.as_mut_ptr().add(i),
            _mm256_add_pd(
                _mm256_loadu_pd(previous.as_ptr().add(i)),
                _mm256_cvtps_pd(_mm_loadu_ps(row.as_ptr().add(i))),
            ),
        );
    }
    for i in end..out.len() {
        out[i] = previous[i] + row[i] as f64;
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn resize_row_avx2(top: &[f32], bottom: &[f32], scale: f32, wy: f32, out: &mut [f32]) {
    use crate::simd::x86::clamp;
    let end = out.len() / 8 * 8;
    let one = _mm256_set1_ps(1.0);
    let last = _mm256_set1_epi32((top.len() - 1) as i32);
    for i in (0..end).step_by(8) {
        let columns = _mm256_setr_ps(
            i as f32,
            (i + 1) as f32,
            (i + 2) as f32,
            (i + 3) as f32,
            (i + 4) as f32,
            (i + 5) as f32,
            (i + 6) as f32,
            (i + 7) as f32,
        );
        let xs = _mm256_sub_ps(
            _mm256_mul_ps(
                _mm256_add_ps(columns, _mm256_set1_ps(0.5)),
                _mm256_set1_ps(scale),
            ),
            _mm256_set1_ps(0.5),
        );
        let xs = clamp(xs, 0.0, top.len() as f32 - 1.0);
        let x0 = _mm256_cvttps_epi32(xs);
        let x1 = _mm256_min_epi32(_mm256_add_epi32(x0, _mm256_set1_epi32(1)), last);
        let wx = _mm256_sub_ps(xs, _mm256_cvtepi32_ps(x0));
        let blend = |row: &[f32]| {
            _mm256_add_ps(
                _mm256_mul_ps(
                    _mm256_i32gather_ps::<4>(row.as_ptr(), x0),
                    _mm256_sub_ps(one, wx),
                ),
                _mm256_mul_ps(_mm256_i32gather_ps::<4>(row.as_ptr(), x1), wx),
            )
        };
        let value = _mm256_add_ps(
            _mm256_mul_ps(blend(top), _mm256_set1_ps(1.0 - wy)),
            _mm256_mul_ps(blend(bottom), _mm256_set1_ps(wy)),
        );
        _mm256_storeu_ps(out.as_mut_ptr().add(i), value);
    }
    resize_row_portable(top, bottom, scale, wy, &mut out[end..], end);
}
