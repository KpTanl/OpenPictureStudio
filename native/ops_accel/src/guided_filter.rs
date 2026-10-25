use rayon::prelude::*;

/// 一维水平方向滑动窗口均值滤波（O(1) 滑动窗口，按行并行）
fn box_filter_horizontal(src: &[f32], dst: &mut [f32], width: usize, r: usize) {
    dst.par_chunks_exact_mut(width)
        .zip(src.par_chunks_exact(width))
        .for_each(|(dst_row, src_row)| {
            let r_i = r as isize;
            let w_i = width as isize;

            let mut sum: f64 = 0.0;
            let c0 = 0.max(-r_i) as usize;
            let c1 = (r_i + 1).min(w_i) as usize;
            for x in c0..c1 {
                sum += src_row[x] as f64;
            }
            let area = (c1 - c0) as f64;
            dst_row[0] = (sum / area) as f32;

            for x in 1..width {
                let x_i = x as isize;
                let add_idx = x_i + r_i;
                let rem_idx = x_i - r_i - 1;

                if add_idx < w_i {
                    sum += src_row[add_idx as usize] as f64;
                }
                if rem_idx >= 0 {
                    sum -= src_row[rem_idx as usize] as f64;
                }

                let cur_c0 = 0.max(x_i - r_i) as usize;
                let cur_c1 = (x_i + r_i + 1).min(w_i) as usize;
                let cur_area = (cur_c1 - cur_c0) as f64;
                dst_row[x] = (sum / cur_area) as f32;
            }
        });
}

/// 一维垂直方向滑动窗口均值滤波（O(1) 滑动窗口，按列块多核并行）
fn box_filter_vertical(src: &[f32], dst: &mut [f32], width: usize, height: usize, r: usize) {
    let r_i = r as isize;
    let h_i = height as isize;

    // 按列块并行（比如每块 64 列），块内维护每一列的滑动窗口和
    let block_size = 64;
    let num_blocks = (width + block_size - 1) / block_size;

    (0..num_blocks).into_par_iter().for_each(|b| {
        let col_start = b * block_size;
        let col_end = (col_start + block_size).min(width);
        let block_w = col_end - col_start;

        let mut col_sums = vec![0.0f64; block_w];

        // 初始化 y = 0 的窗口和
        let r0 = 0.max(-r_i) as usize;
        let r1 = (r_i + 1).min(h_i) as usize;
        for y in r0..r1 {
            let row_offset = y * width;
            for (i, c) in (col_start..col_end).enumerate() {
                col_sums[i] += src[row_offset + c] as f64;
            }
        }

        let area0 = (r1 - r0) as f64;
        let dst_ptr = dst.as_ptr() as usize; // 安全的无重叠分块写入

        unsafe {
            let dst_raw = dst_ptr as *mut f32;
            for (i, c) in (col_start..col_end).enumerate() {
                *dst_raw.add(c) = (col_sums[i] / area0) as f32;
            }

            for y in 1..height {
                let y_i = y as isize;
                let add_y = y_i + r_i;
                let rem_y = y_i - r_i - 1;

                if add_y < h_i {
                    let add_offset = add_y as usize * width;
                    for (i, c) in (col_start..col_end).enumerate() {
                        col_sums[i] += src[add_offset + c] as f64;
                    }
                }

                if rem_y >= 0 {
                    let rem_offset = rem_y as usize * width;
                    for (i, c) in (col_start..col_end).enumerate() {
                        col_sums[i] -= src[rem_offset + c] as f64;
                    }
                }

                let cur_r0 = 0.max(y_i - r_i) as usize;
                let cur_r1 = (y_i + r_i + 1).min(h_i) as usize;
                let cur_area = (cur_r1 - cur_r0) as f64;

                let row_offset = y * width;
                for (i, c) in (col_start..col_end).enumerate() {
                    *dst_raw.add(row_offset + c) = (col_sums[i] / cur_area) as f32;
                }
            }
        }
    });
}

/// 2D BoxFilter（纯 O(1) 滑动窗口多核滤波）
pub fn box_filter_2d(src: &[f32], width: usize, height: usize, radius: usize) -> Vec<f32> {
    if radius == 0 {
        return src.to_vec();
    }
    let mut temp = vec![0.0f32; width * height];
    let mut dst = vec![0.0f32; width * height];

    box_filter_horizontal(src, &mut temp, width, radius);
    box_filter_vertical(&temp, &mut dst, width, height, radius);

    dst
}

/// 多核双线性插值缩放（单通道）
pub fn resize_bilinear_1channel(
    src: &[f32],
    in_w: usize,
    in_h: usize,
    out_w: usize,
    out_h: usize,
) -> Vec<f32> {
    if in_w == out_w && in_h == out_h {
        return src.to_vec();
    }

    let mut dst = vec![0.0f32; out_w * out_h];
    let in_w_f = in_w as f32;
    let in_h_f = in_h as f32;
    let out_w_f = out_w as f32;
    let out_h_f = out_h as f32;

    dst.par_chunks_exact_mut(out_w)
        .enumerate()
        .for_each(|(y_out, row)| {
            let ys = (y_out as f32 + 0.5) * (in_h_f / out_h_f) - 0.5;
            let ys_clamped = ys.clamp(0.0, in_h_f - 1.0);
            let y0 = ys_clamped.floor() as usize;
            let y1 = (y0 + 1).min(in_h - 1);
            let wy = ys_clamped - y0 as f32;

            for x_out in 0..out_w {
                let xs = (x_out as f32 + 0.5) * (in_w_f / out_w_f) - 0.5;
                let xs_clamped = xs.clamp(0.0, in_w_f - 1.0);
                let x0 = xs_clamped.floor() as usize;
                let x1 = (x0 + 1).min(in_w - 1);
                let wx = xs_clamped - x0 as f32;

                let ia = src[y0 * in_w + x0];
                let ib = src[y0 * in_w + x1];
                let ic = src[y1 * in_w + x0];
                let id = src[y1 * in_w + x1];

                let top = ia * (1.0 - wx) + ib * wx;
                let bot = ic * (1.0 - wx) + id * wx;

                row[x_out] = top * (1.0 - wy) + bot * wy;
            }
        });

    dst
}

/// Fast Guided Filter (He et al. 2015) — 多核端到端并行
pub fn fast_guided_filter(
    guide: &[f32],
    src: &[f32],
    width: usize,
    height: usize,
    radius: usize,
    eps: f32,
    subsample: usize,
) -> Vec<f32> {
    let s = subsample.max(1);

    if s == 1 {
        let mean_g = box_filter_2d(guide, width, height, radius);
        let mean_s = box_filter_2d(src, width, height, radius);

        let g_sq: Vec<f32> = guide.par_iter().map(|&g| g * g).collect();
        let g_src: Vec<f32> = guide.par_iter().zip(src.par_iter()).map(|(&g, &p)| g * p).collect();

        let corr_g = box_filter_2d(&g_sq, width, height, radius);
        let corr_gs = box_filter_2d(&g_src, width, height, radius);

        let mut a = vec![0.0f32; width * height];
        let mut b = vec![0.0f32; width * height];

        a.par_iter_mut()
            .zip(b.par_iter_mut())
            .zip(mean_g.par_iter())
            .zip(mean_s.par_iter())
            .zip(corr_g.par_iter())
            .zip(corr_gs.par_iter())
            .for_each(|(((((out_a, out_b), &mg), &ms), &cg), &cgs)| {
                let var_g = cg - mg * mg;
                let cov_gs = cgs - mg * ms;
                let a_val = cov_gs / (var_g + eps);
                let b_val = ms - a_val * mg;
                *out_a = a_val;
                *out_b = b_val;
            });

        let mean_a = box_filter_2d(&a, width, height, radius);
        let mean_b = box_filter_2d(&b, width, height, radius);

        let mut out = vec![0.0f32; width * height];
        out.par_iter_mut()
            .zip(guide.par_iter())
            .zip(mean_a.par_iter())
            .zip(mean_b.par_iter())
            .for_each(|(((o, &g), &ma), &mb)| {
                *o = ma * g + mb;
            });

        return out;
    }

    let sw = (width / s).max(1);
    let sh = (height / s).max(1);
    let r_sub = (radius / s).max(1);

    let g_sub = resize_bilinear_1channel(guide, width, height, sw, sh);
    let s_sub = resize_bilinear_1channel(src, width, height, sw, sh);

    let mean_g = box_filter_2d(&g_sub, sw, sh, r_sub);
    let mean_s = box_filter_2d(&s_sub, sw, sh, r_sub);

    let g_sq: Vec<f32> = g_sub.par_iter().map(|&g| g * g).collect();
    let g_src: Vec<f32> = g_sub.par_iter().zip(s_sub.par_iter()).map(|(&g, &p)| g * p).collect();

    let corr_g = box_filter_2d(&g_sq, sw, sh, r_sub);
    let corr_gs = box_filter_2d(&g_src, sw, sh, r_sub);

    let sub_len = sw * sh;
    let mut a_sub = vec![0.0f32; sub_len];
    let mut b_sub = vec![0.0f32; sub_len];

    a_sub
        .par_iter_mut()
        .zip(b_sub.par_iter_mut())
        .zip(mean_g.par_iter())
        .zip(mean_s.par_iter())
        .zip(corr_g.par_iter())
        .zip(corr_gs.par_iter())
        .for_each(|(((((out_a, out_b), &mg), &ms), &cg), &cgs)| {
            let var_g = cg - mg * mg;
            let cov_gs = cgs - mg * ms;
            let a_val = cov_gs / (var_g + eps);
            let b_val = ms - a_val * mg;
            *out_a = a_val;
            *out_b = b_val;
        });

    let mean_a_sub = box_filter_2d(&a_sub, sw, sh, r_sub);
    let mean_b_sub = box_filter_2d(&b_sub, sw, sh, r_sub);

    let mean_a = resize_bilinear_1channel(&mean_a_sub, sw, sh, width, height);
    let mean_b = resize_bilinear_1channel(&mean_b_sub, sw, sh, width, height);

    let mut out = vec![0.0f32; width * height];
    out.par_iter_mut()
        .zip(guide.par_iter())
        .zip(mean_a.par_iter())
        .zip(mean_b.par_iter())
        .for_each(|(((o, &g), &ma), &mb)| {
            *o = ma * g + mb;
        });

    out
}
