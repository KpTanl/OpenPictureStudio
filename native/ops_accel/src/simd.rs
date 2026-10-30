use pyo3::exceptions::PyValueError;
use pyo3::PyResult;
use std::sync::atomic::{AtomicU32, Ordering};

#[derive(Clone, Copy)]
#[repr(u32)]
pub enum Operator {
    Tone = 1,
    ColorSpace = 2,
    Curves = 4,
    Local = 8,
    Raw = 16,
    Filter = 32,
    Display = 64,
}

const ALL_AVX2: u32 = 127;
static ENABLED: AtomicU32 = AtomicU32::new(0);

pub fn available() -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        std::is_x86_feature_detected!("avx2")
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        false
    }
}

pub fn initialize() -> PyResult<&'static str> {
    let value = std::env::var("OPS_CPU_KERNEL").unwrap_or_default();
    let mask = match value.as_str() {
        "" | "auto" => {
            if available() {
                ALL_AVX2
            } else {
                0
            }
        }
        "portable" => 0,
        "avx2" if available() => ALL_AVX2,
        "avx2" => return Err(PyValueError::new_err("OPS_CPU_KERNEL=avx2 requires AVX2")),
        _ => {
            return Err(PyValueError::new_err(
                "OPS_CPU_KERNEL must be auto, portable or avx2",
            ))
        }
    };
    ENABLED.store(mask, Ordering::Relaxed);
    Ok(if mask == 0 { "portable" } else { "avx2" })
}

#[inline]
pub fn enabled(operator: Operator) -> bool {
    ENABLED.load(Ordering::Relaxed) & operator as u32 != 0
}

pub fn operators() -> Vec<&'static str> {
    [
        (Operator::Tone, "tone"),
        (Operator::ColorSpace, "color_space"),
        (Operator::Curves, "curves"),
        (Operator::Local, "local"),
        (Operator::Raw, "raw"),
        (Operator::Filter, "filter"),
        (Operator::Display, "display"),
    ]
    .into_iter()
    .filter_map(|(op, name)| enabled(op).then_some(name))
    .collect()
}

#[cfg(target_arch = "x86_64")]
pub mod x86 {
    use std::arch::x86_64::*;

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn select(mask: __m256, yes: __m256, no: __m256) -> __m256 {
        _mm256_blendv_ps(no, yes, mask)
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn clamp(value: __m256, low: f32, high: f32) -> __m256 {
        let lo = _mm256_set1_ps(low);
        let hi = _mm256_set1_ps(high);
        let value = select(_mm256_cmp_ps::<_CMP_LT_OQ>(value, lo), lo, value);
        select(_mm256_cmp_ps::<_CMP_GT_OQ>(value, hi), hi, value)
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn max(a: __m256, b: __m256) -> __m256 {
        select(
            _mm256_or_ps(
                _mm256_cmp_ps::<_CMP_LT_OQ>(a, b),
                _mm256_cmp_ps::<_CMP_UNORD_Q>(a, a),
            ),
            b,
            a,
        )
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn min(a: __m256, b: __m256) -> __m256 {
        select(
            _mm256_or_ps(
                _mm256_cmp_ps::<_CMP_GT_OQ>(a, b),
                _mm256_cmp_ps::<_CMP_UNORD_Q>(a, a),
            ),
            b,
            a,
        )
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn luma(r: __m256, g: __m256, b: __m256) -> __m256 {
        _mm256_add_ps(
            _mm256_add_ps(
                _mm256_mul_ps(r, _mm256_set1_ps(0.2126)),
                _mm256_mul_ps(g, _mm256_set1_ps(0.7152)),
            ),
            _mm256_mul_ps(b, _mm256_set1_ps(0.0722)),
        )
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn load_rgb(ptr: *const f32) -> [__m256; 3] {
        let a = _mm256_loadu_ps(ptr);
        let b = _mm256_loadu_ps(ptr.add(8));
        let c = _mm256_loadu_ps(ptr.add(16));
        unpack_rgb(a, b, c)
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn unpack_rgb(a: __m256, b: __m256, c: __m256) -> [__m256; 3] {
        let r = _mm256_permutevar8x32_ps(a, _mm256_setr_epi32(0, 3, 6, 0, 0, 0, 0, 0));
        let r = _mm256_blend_ps::<0x38>(
            r,
            _mm256_permutevar8x32_ps(b, _mm256_setr_epi32(0, 0, 0, 1, 4, 7, 0, 0)),
        );
        let r = _mm256_blend_ps::<0xc0>(
            r,
            _mm256_permutevar8x32_ps(c, _mm256_setr_epi32(0, 0, 0, 0, 0, 0, 2, 5)),
        );
        let g = _mm256_permutevar8x32_ps(a, _mm256_setr_epi32(1, 4, 7, 0, 0, 0, 0, 0));
        let g = _mm256_blend_ps::<0x18>(
            g,
            _mm256_permutevar8x32_ps(b, _mm256_setr_epi32(0, 0, 0, 2, 5, 0, 0, 0)),
        );
        let g = _mm256_blend_ps::<0xe0>(
            g,
            _mm256_permutevar8x32_ps(c, _mm256_setr_epi32(0, 0, 0, 0, 0, 0, 3, 6)),
        );
        let b0 = _mm256_permutevar8x32_ps(a, _mm256_setr_epi32(2, 5, 0, 0, 0, 0, 0, 0));
        let b0 = _mm256_blend_ps::<0x1c>(
            b0,
            _mm256_permutevar8x32_ps(b, _mm256_setr_epi32(0, 0, 0, 3, 6, 0, 0, 0)),
        );
        let b0 = _mm256_blend_ps::<0xe0>(
            b0,
            _mm256_permutevar8x32_ps(c, _mm256_setr_epi32(0, 0, 0, 0, 0, 1, 4, 7)),
        );
        [r, g, b0]
    }

    #[inline]
    #[target_feature(enable = "avx2")]
    pub unsafe fn store_rgb(ptr: *mut f32, [r, g, b]: [__m256; 3]) {
        let a = _mm256_permutevar8x32_ps(r, _mm256_setr_epi32(0, 0, 0, 1, 0, 0, 2, 0));
        let a = _mm256_blend_ps::<0x92>(
            a,
            _mm256_permutevar8x32_ps(g, _mm256_setr_epi32(0, 0, 0, 0, 1, 0, 0, 2)),
        );
        let a = _mm256_blend_ps::<0x24>(
            a,
            _mm256_permutevar8x32_ps(b, _mm256_setr_epi32(0, 0, 0, 0, 0, 1, 0, 0)),
        );
        let m = _mm256_permutevar8x32_ps(r, _mm256_setr_epi32(0, 3, 0, 0, 4, 0, 0, 5));
        let m = _mm256_blend_ps::<0x24>(
            m,
            _mm256_permutevar8x32_ps(g, _mm256_setr_epi32(0, 0, 3, 0, 0, 4, 0, 0)),
        );
        let m = _mm256_blend_ps::<0x49>(
            m,
            _mm256_permutevar8x32_ps(b, _mm256_setr_epi32(2, 0, 0, 3, 0, 0, 4, 0)),
        );
        let z = _mm256_permutevar8x32_ps(r, _mm256_setr_epi32(0, 0, 6, 0, 0, 7, 0, 0));
        let z = _mm256_blend_ps::<0x49>(
            z,
            _mm256_permutevar8x32_ps(g, _mm256_setr_epi32(5, 0, 0, 6, 0, 0, 7, 0)),
        );
        let z = _mm256_blend_ps::<0x92>(
            z,
            _mm256_permutevar8x32_ps(b, _mm256_setr_epi32(0, 5, 0, 0, 6, 0, 0, 7)),
        );
        _mm256_storeu_ps(ptr, a);
        _mm256_storeu_ps(ptr.add(8), m);
        _mm256_storeu_ps(ptr.add(16), z);
    }
}
