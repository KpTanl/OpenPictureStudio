use numpy::{PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::prelude::*;

pub type PatchInput<'py> = (usize, usize, PyReadonlyArray3<'py, f32>, Option<PyReadonlyArray2<'py, f32>>);

pub struct Patch<'a> {
    left: usize,
    top: usize,
    width: usize,
    height: usize,
    rgb: &'a [f32],
    alpha: Option<&'a [f32]>,
}

pub struct ImageView<'a> {
    pub width: usize,
    pub height: usize,
    pub rgb: &'a [f32],
    pub alpha: Option<&'a [f32]>,
    pub patches: Vec<Patch<'a>>,
}

impl<'a> ImageView<'a> {
    pub fn new<'py>(pixels: &'a PyReadonlyArray3<'py, f32>,
        alpha: Option<&'a PyReadonlyArray2<'py, f32>>, patches: &'a [PatchInput<'py>]) -> PyResult<Self> {
        let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
        if width == 0 || height == 0 || pixels.shape()[2] != 3
            || alpha.is_some_and(|a| a.shape() != [height, width]) {
            return Err(pyo3::exceptions::PyValueError::new_err("expected RGB and matching Alpha"));
        }
        let mut layers = Vec::with_capacity(patches.len());
        for (left, top, rgb, coverage) in patches {
            let (ph, pw) = (rgb.shape()[0], rgb.shape()[1]);
            if pw == 0 || ph == 0 || rgb.shape()[2] != 3 || *left > width || *top > height
                || pw > width - *left || ph > height - *top
                || coverage.as_ref().is_some_and(|a| alpha.is_none() || a.shape() != [ph, pw]) {
                return Err(pyo3::exceptions::PyValueError::new_err("invalid image patch"));
            }
            layers.push(Patch { left: *left, top: *top, width: pw, height: ph, rgb: rgb.as_slice()?,
                alpha: coverage.as_ref().map(|a| a.as_slice()).transpose()? });
        }
        Ok(Self { width, height, rgb: pixels.as_slice()?,
            alpha: alpha.map(|a| a.as_slice()).transpose()?, patches: layers })
    }

    #[inline]
    pub fn pixel(&self, x: usize, y: usize) -> ([f32; 3], f32) {
        let at = y * self.width + x;
        let mut rgb = [self.rgb[at * 3], self.rgb[at * 3 + 1], self.rgb[at * 3 + 2]];
        let mut alpha = self.alpha.map_or(1.0, |a| a[at]);
        for patch in &self.patches {
            if x >= patch.left && y >= patch.top && x - patch.left < patch.width && y - patch.top < patch.height {
                let index = (y - patch.top) * patch.width + x - patch.left;
                rgb.copy_from_slice(&patch.rgb[index * 3..index * 3 + 3]);
                if let Some(values) = patch.alpha { alpha = values[index]; }
            }
        }
        (rgb, alpha)
    }
}
