def needs_detail(display_size, device_pixel_ratio, proxy_size, full_size):
    return any(full > proxy and shown * device_pixel_ratio > proxy
               for shown, proxy, full in zip(display_size, proxy_size, full_size))
