# System Requirements and Performance

[简体中文](performance.md) | [English](performance.en.md) | [Français](performance.fr.md)

[Back to README](../README.en.md)

## Configuration Suggestions

| Item | Suggested for basic use | Recommended | Better |
| --- | --- | --- | --- |
| OS | Windows 10/11, 64-bit | Windows 10/11, 64-bit | Windows 10/11, 64-bit |
| CPU | x64, modern multicore CPU | 7th-gen Intel i5 or 1st-gen Ryzen, or better | 12th-gen Intel or 5th-gen Ryzen, or better |
| RAM | 8 GB, suited to browsing and light editing | 16 GB, suited to RAW and multi-tool editing | 32 GB or more, suited to high-resolution photos, many masks, and batch processing |
| GPU | Integrated graphics | GTX 950 or better | RTX 2060 or better |
| Storage | 3 GB free | 6 GB free, SSD recommended | 12 GB free, SSD recommended |

The installed version includes its runtime dependencies. AI models are downloaded as needed, and the AI retouching assistant needs an online service to be configured.

## Image Processing Performance

Test computer: Intel Core i5-12600KF, 32 GB RAM, Windows 11. Shorter times are better.

### Python vs. Rust

#### RAW Color Conversion

Using the decoded result of the same Nikon Z 50 RAW file, this compares the color conversion time of Python/NumPy and 8-thread Rust. After warm-up, the two were measured alternately 9 times, and the median is reported. Test date: October 5, 2026.

| Task | Image size | Python/NumPy | Rust |
| --- | --- | ---: | ---: |
| RAW color conversion | 1864×2800 | 95.0 ms | 7.7 ms |
| RAW color conversion | 3728×5600 | 371.3 ms | 29.4 ms |

#### Portrait Retouching

The same portrait and the same retouching settings, with one face processed per frame, and Rust using 8 threads. Python/NumPy was measured over 10 frames and Rust over 30 frames. The table shows the median, and includes the time from processing to producing the preview image. Test date: October 25, 2026.

| Task | Image size | Python/NumPy | Rust |
| --- | --- | ---: | ---: |
| Common retouching | 3840×2560 | 1.956 s | 1.148 s |
| Retouching plus face-shape adjustment | 3840×2560 | 2.652 s | 1.854 s |
| Common retouching | 6720×4480 | 6.211 s | 3.411 s |
| Retouching plus face-shape adjustment | 6720×4480 | 7.425 s | 4.692 s |

#### Local Masks

Using a 1600×1067 synthetic image with a combined tone, color, and detail adjustment, and Rust using 10 threads. This data comes from the mask prototype: Python is a single result after warm-up. For Rust, the 1, 3, and 8 mask cases are the median of 30 runs, and the 128 mask case is a single result. Test date: October 19, 2026.

| Number of masks | Python | Rust |
| --- | ---: | ---: |
| 1 mask | 2419.629 ms | 70.750 ms |
| 3 masks | 2747.778 ms | 94.494 ms |
| 8 masks | 3121.199 ms | 119.038 ms |
| 128 masks | 13245.931 ms | 2984.170 ms |

### Rust vs. Rust + AVX2

Using a 6000×4000 synthetic image, the same processing was measured with 8 threads, and the median of repeated samples is reported. Test date: October 29, 2026.

| Task | Rust | Rust + AVX2 |
| --- | ---: | ---: |
| Image display conversion | 30.39 ms | 18.95 ms |
| Basic color grading | 101.63 ms | 64.35 ms |
| Curves and color mixing | 382.15 ms | 296.51 ms |
| Spatial color grading | 1459.20 ms | 633.68 ms |
| Local adjustment with three masks | 516.26 ms | 415.92 ms |
| RAW color conversion | 23.03 ms | 17.07 ms |

Real-world speed varies with your computer, the photo, and the adjustments. AVX2 is implemented in the source, and the matching installer is waiting for release acceptance.
