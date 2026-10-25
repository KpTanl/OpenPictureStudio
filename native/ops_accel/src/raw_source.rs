use rayon::prelude::*;

pub fn raw_xyz_to_linear_srgb(source: &[u16], maxval: f32, matrix: [[f64; 3]; 3],
                            width: usize, out: &mut [f32]) {
    if width == 0 || out.is_empty() {
        return;
    }
    out.par_chunks_mut(width * 3 * 16)
        .zip(source.par_chunks(width * 3 * 16))
        .for_each(|(target, input)| {
            for (output, pixel) in target.chunks_exact_mut(3).zip(input.chunks_exact(3)) {
                let x = (pixel[0] as f32 / maxval) as f64;
                let y = (pixel[1] as f32 / maxval) as f64;
                let z = (pixel[2] as f32 / maxval) as f64;
                for channel in 0..3 {
                    output[channel] = (x * matrix[channel][0] + y * matrix[channel][1]
                        + z * matrix[channel][2]) as f32;
                }
            }
        });
}
