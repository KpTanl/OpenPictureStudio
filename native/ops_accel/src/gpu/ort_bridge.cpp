#include "dml_provider_factory.h"
#include <algorithm>
#include <exception>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <wrl/client.h>

struct OpsOrt {
    const OrtApi* api = nullptr;
    const OrtDmlApi* dml = nullptr;
    OrtEnv* env = nullptr;
    OrtSession* session = nullptr;
    OrtMemoryInfo* memory = nullptr;
    OrtValue* input = nullptr;
    std::vector<OrtValue*> outputs;
    void* input_allocation = nullptr;
    std::vector<void*> output_allocations;
    std::string input_name;
    std::vector<std::string> output_names;
    Microsoft::WRL::ComPtr<ID3D12CommandQueue> queue;
    Microsoft::WRL::ComPtr<ID3D12CommandAllocator> allocators[2];
    Microsoft::WRL::ComPtr<ID3D12GraphicsCommandList> barriers[2];

    void barrier(size_t index) {
        if (FAILED(allocators[index]->Reset()) || FAILED(barriers[index]->Reset(allocators[index].Get(), nullptr)))
            throw std::runtime_error("DirectML synchronization reset failed");
        D3D12_RESOURCE_BARRIER fence = {};
        fence.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        barriers[index]->ResourceBarrier(1, &fence);
        if (FAILED(barriers[index]->Close())) throw std::runtime_error("DirectML synchronization close failed");
        ID3D12CommandList* commands[] = {barriers[index].Get()};
        queue->ExecuteCommandLists(1, commands);
    }

    void check(OrtStatus* status) {
        if (!status) return;
        std::string message = api->GetErrorMessage(status);
        api->ReleaseStatus(status);
        throw std::runtime_error(message);
    }

    ~OpsOrt() {
        if (!api) return;
        api->ReleaseValue(input);
        for (auto value : outputs) api->ReleaseValue(value);
        if (input_allocation) { auto status = dml->FreeGPUAllocation(input_allocation); api->ReleaseStatus(status); }
        for (auto allocation : output_allocations) if (allocation) { auto status = dml->FreeGPUAllocation(allocation); api->ReleaseStatus(status); }
        api->ReleaseSession(session);
        api->ReleaseMemoryInfo(memory);
        api->ReleaseEnv(env);
    }

    void validate_type(bool is_input, const int64_t* shape, size_t rank, size_t index = 0) {
        OrtTypeInfo* type = nullptr;
        check(is_input ? api->SessionGetInputTypeInfo(session, index, &type) : api->SessionGetOutputTypeInfo(session, index, &type));
        try {
            const OrtTensorTypeAndShapeInfo* tensor = nullptr;
            check(api->CastTypeInfoToTensorInfo(type, &tensor));
            if (!tensor) throw std::runtime_error("Model output is not a tensor");
            ONNXTensorElementDataType dtype;
            check(api->GetTensorElementType(tensor, &dtype));
            size_t dimensions;
            check(api->GetDimensionsCount(tensor, &dimensions));
            if (dtype != ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT || dimensions != rank)
                throw std::runtime_error("Model requires a different tensor type or rank");
            std::vector<int64_t> declared(rank);
            check(api->GetDimensions(tensor, declared.data(), rank));
            for (size_t i = 0; i < rank; ++i)
                if (declared[i] > 0 && declared[i] != shape[i]) throw std::runtime_error("Model tensor shape mismatch");
        } catch (...) { api->ReleaseTypeInfo(type); throw; }
        api->ReleaseTypeInfo(type);
    }
};

static void message(char* buffer, size_t capacity, const char* text) {
    if (capacity) {
        size_t length = std::min(capacity - 1, strlen(text));
        memcpy(buffer, text, length);
        buffer[length] = 0;
    }
}

extern "C" int ops_ort_open_tensors(const OrtApiBase* base, IDMLDevice* device, ID3D12CommandQueue* queue,
    const wchar_t* path, ID3D12Resource* input, ID3D12Resource* const* output_resources,
    const int64_t* input_shape, const int64_t* const* output_shapes, const size_t* output_ranks,
    size_t input_bytes, const size_t* output_bytes, size_t output_count,
    OpsOrt** result, char* error, size_t error_capacity) noexcept {
    try {
        auto bridge = std::make_unique<OpsOrt>();
        bridge->api = base->GetApi(ORT_API_VERSION);
        if (!bridge->api) throw std::runtime_error("ONNX Runtime C API 24 or newer is required");
        auto api = bridge->api;
        bridge->queue = queue;
        Microsoft::WRL::ComPtr<ID3D12Device> native_device;
        if (FAILED(queue->GetDevice(IID_PPV_ARGS(native_device.GetAddressOf()))))
            throw std::runtime_error("DirectML queue device is unavailable");
        for (size_t index = 0; index < 2; ++index) {
            if (FAILED(native_device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                    IID_PPV_ARGS(bridge->allocators[index].GetAddressOf()))) ||
                FAILED(native_device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, bridge->allocators[index].Get(),
                    nullptr, IID_PPV_ARGS(bridge->barriers[index].GetAddressOf()))) ||
                FAILED(bridge->barriers[index]->Close())) throw std::runtime_error("DirectML synchronization creation failed");
        }
        bridge->check(api->GetExecutionProviderApi("DML", ORT_API_VERSION, reinterpret_cast<const void**>(&bridge->dml)));
        bridge->check(api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "OPS", &bridge->env));
        OrtSessionOptions* options = nullptr;
        bridge->check(api->CreateSessionOptions(&options));
        try {
            bridge->check(api->DisableMemPattern(options));
            bridge->check(api->SetSessionExecutionMode(options, ORT_SEQUENTIAL));
            bridge->check(api->SetIntraOpNumThreads(options, 1));
            bridge->check(api->SetInterOpNumThreads(options, 1));
            bridge->check(api->AddSessionConfigEntry(options, "session.disable_cpu_ep_fallback", "1"));
            for (size_t axis = 0; axis < 4; ++axis) {
                std::string dimension = "ops_input_" + std::to_string(axis);
                bridge->check(api->AddFreeDimensionOverrideByName(options, dimension.c_str(), input_shape[axis]));
            }
            bridge->check(bridge->dml->SessionOptionsAppendExecutionProvider_DML1(options, device, queue));
            bridge->check(api->CreateSession(bridge->env, path, options, &bridge->session));
        } catch (...) { api->ReleaseSessionOptions(options); throw; }
        api->ReleaseSessionOptions(options);
        size_t inputs = 0, outputs = 0;
        bridge->check(api->SessionGetInputCount(bridge->session, &inputs));
        bridge->check(api->SessionGetOutputCount(bridge->session, &outputs));
        if (inputs != 1 || outputs != output_count || outputs == 0 || outputs > 32)
            throw std::runtime_error("GPU tensor adapter input or output count mismatch");
        bridge->validate_type(true, input_shape, 4);
        OrtAllocator* allocator = nullptr;
        bridge->check(api->GetAllocatorWithDefaultOptions(&allocator));
        char* name = nullptr;
        bridge->check(api->SessionGetInputName(bridge->session, 0, allocator, &name));
        bridge->input_name = name;
        allocator->Free(allocator, name);
        bridge->check(api->CreateMemoryInfo("DML", OrtDeviceAllocator, 0, OrtMemTypeDefault, &bridge->memory));
        bridge->check(bridge->dml->CreateGPUAllocationFromD3DResource(input, &bridge->input_allocation));
        bridge->check(api->CreateTensorWithDataAsOrtValue(bridge->memory, bridge->input_allocation, input_bytes,
            input_shape, 4, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &bridge->input));
        bridge->outputs.resize(outputs, nullptr);
        bridge->output_allocations.resize(outputs, nullptr);
        bridge->output_names.reserve(outputs);
        for (size_t i = 0; i < outputs; ++i) {
            bridge->validate_type(false, output_shapes[i], output_ranks[i], i);
            name = nullptr;
            bridge->check(api->SessionGetOutputName(bridge->session, i, allocator, &name));
            bridge->output_names.emplace_back(name);
            allocator->Free(allocator, name);
            bridge->check(bridge->dml->CreateGPUAllocationFromD3DResource(output_resources[i], &bridge->output_allocations[i]));
            bridge->check(api->CreateTensorWithDataAsOrtValue(bridge->memory, bridge->output_allocations[i], output_bytes[i],
                output_shapes[i], output_ranks[i], ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &bridge->outputs[i]));
        }
        *result = bridge.release();
        return 0;
    } catch (const std::exception& reason) { message(error, error_capacity, reason.what()); return -1; }
    catch (...) { message(error, error_capacity, "ONNX Runtime session creation failed"); return -1; }
}

extern "C" int ops_ort_open(const OrtApiBase* base, IDMLDevice* device, ID3D12CommandQueue* queue,
    const wchar_t* path, ID3D12Resource* input, ID3D12Resource* output,
    const int64_t* input_shape, const int64_t* output_shape, size_t input_bytes, size_t output_bytes,
    OpsOrt** result, char* error, size_t capacity) noexcept {
    const size_t rank = 4;
    return ops_ort_open_tensors(base, device, queue, path, input, &output, input_shape, &output_shape,
        &rank, input_bytes, &output_bytes, 1, result, error, capacity);
}

extern "C" int ops_ort_run(OpsOrt* bridge, char* error, size_t capacity) noexcept {
    try {
        const char* input_names[] = {bridge->input_name.c_str()};
        std::vector<const char*> output_names;
        for (auto& name : bridge->output_names) output_names.push_back(name.c_str());
        bridge->barrier(0);
        auto status = bridge->api->Run(bridge->session, nullptr, input_names, &bridge->input, 1,
            output_names.data(), output_names.size(), bridge->outputs.data());
        bridge->barrier(1);
        bridge->check(status);
        return 0;
    } catch (const std::exception& reason) { message(error, capacity, reason.what()); return -1; }
    catch (...) { message(error, capacity, "ONNX Runtime execution failed"); return -1; }
}

extern "C" void ops_ort_close(OpsOrt* bridge) noexcept { delete bridge; }
