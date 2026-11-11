fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        println!("cargo:rerun-if-env-changed=OPS_ATL_LIB");
        if std::env::var_os("CARGO_FEATURE_STATIC_DXC").is_some() {
            if let Some(path) = std::env::var_os("OPS_ATL_LIB") {
                let path = std::path::PathBuf::from(path);
                assert!(path.join("atls.lib").is_file(), "OPS_ATL_LIB must contain the MSVC ATL library atls.lib");
                println!("cargo:rustc-link-search=native={}", path.display());
            }
        }
        println!("cargo:rerun-if-changed=src/gpu/ort_bridge.cpp");
        println!("cargo:rerun-if-changed=vendor/onnxruntime");
        cc::Build::new().cpp(true).define("NOMINMAX", None).file("src/gpu/ort_bridge.cpp").include("vendor/onnxruntime")
            .flag_if_supported("/std:c++17").flag_if_supported("/EHsc").flag_if_supported("/utf-8")
            .compile("ops_ort_bridge");
    }
    println!("cargo:rerun-if-changed=src/gpu/shaders");
    for entry in std::fs::read_dir("src/gpu/shaders").unwrap() {
        let path = entry.unwrap().path();
        println!("cargo:rerun-if-changed={}", path.display());
        let text = std::fs::read_to_string(&path).unwrap();
        let module = naga::front::wgsl::parse_str(&text)
            .unwrap_or_else(|error| panic!("{}: {}", path.display(), error.emit_to_string(&text)));
        naga::valid::Validator::new(naga::valid::ValidationFlags::all(), naga::valid::Capabilities::empty())
            .validate(&module).unwrap_or_else(|error| panic!("{}: {:?}", path.display(), error));
    }
}
