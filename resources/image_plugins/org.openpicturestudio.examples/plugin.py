import json
import sys

import numpy as np


request = json.load(sys.stdin)
if request["protocol"] != "image/1" or request["api_version"] != 1:
    raise ValueError("图像接口版本无效")
source = request["input"]
pixels = np.load(source["pixels"], allow_pickle=False)
params = request["params"]
if request["node_id"] == "exposure":
    pixels = pixels*np.float32(2**params["stops"])
else:
    rng = np.random.default_rng(params.get("seed"))
    pixels = pixels+rng.standard_normal(pixels.shape, dtype=np.float32)*np.float32(params["amount"])
np.save(request["output"]["pixels"], pixels.astype(np.float32), allow_pickle=False)
if source["alpha"] is not None:
    np.save(request["output"]["alpha"], np.load(source["alpha"], allow_pickle=False), allow_pickle=False)
print(json.dumps({"protocol": "image/1", "status": "ok", "alpha": source["alpha"] is not None}))
