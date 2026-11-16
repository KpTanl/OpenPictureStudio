from dataclasses import replace
from math import prod, ceil, floor

from ..core.contracts.beauty import BEAUTY_MODEL_FIELDS, BeautyEffectStatus
from ..core.contracts.gpu import GpuExecutionError
from ..core.face_regions import OVAL, EYES, BROWS, LIPS, NOSTRILS, JAW
from ..core.beauty_detail_v3 import UNDER_EYE_CHAINS
from .gpu_geometry import GpuGeometry
from .gpu_image_ops import GpuImageOps


COLOR_KEYS = ("skin_smooth", "skin_brighten", "skin_rosy", "sharpen", "teeth_whiten", "spot_remove", "eye_bag_remove")
WARP_KEYS = ("face_slim", "eye_enlarge", "eye_distance", "nose_slim")
BATCH_BYTES = 16 * 1024 ** 2
# 眼袋单个采样点的像素数上限，也是原生按行中位数的列数上限。人脸区域到 400 万像素的上限时单点约 7000 个
ROW_SAMPLES = 32768


class GpuBeauty:
    def __init__(self, backend, cancelled):
        self.ops = GpuImageOps(backend, cancelled)
        self.backend, self.cancelled = backend, cancelled

    def retained(self, name, part, key, compute, *, pixels=True):
        """与美颜强度无关的结果按槽位保留最近一份：输入没变时直接取用，拖动美颜滑块不重算。

        compute 返回元组，首项是承载这份结果的 GPU 图像。pixels=False 表示结果只取决于人脸关键点。"""
        slot = (*self.slot, name, part)
        key = (self.pixels if pixels else self.landmarks, key)
        kept = self.backend.retained(slot, key)
        if kept is None:
            kept = compute()
            self.backend.retain(slot, key, kept[0], kept)
        return kept

    def regions(self, snapshot, index, size, required):
        """一张人脸的各区域蒙版。只取决于关键点：按人脸保留，拖动时不再逐帧重建，也不再读回它的范围。"""
        names = tuple(sorted(required))
        slot, key = (*self.slot, "regions", index), (self.landmarks, names)
        kept = self.backend.retained(slot, key) if names else None
        if kept is None:
            kept = snapshot.regions(index, size, self.cancelled, required=required)
            masks = [getattr(kept, name) for name in names]
            if masks:
                self.backend.retain(slot, key, masks[0], kept)
            # 其余蒙版由上面那份结果带着存活，各登记一个槽位只为把它们计入显存
            for name, mask in zip(names[1:], masks[1:]):
                self.backend.retain((*slot, name), key, mask, None)
        return kept

    def distribution(self, image, mask):
        ops = self.ops
        samples, count = ops.selected(image, mask, limit=4096)
        if count < 32:
            return None
        filtered, center = ops.robust(samples, (4, 2, 2), 3)
        count_image = ops.channel(ops.view(center, (0, 0, 1, 1)), 3)
        if ops.control(count_image)[0, 0, 0] < 32:
            return None
        covariance = ops.stage(filtered, 6, auxiliary=center, size=(4, 1), kernel="beauty_stats")
        return ops.stage(covariance, 7, size=(5, 1), kernel="beauty_stats")

    def skin_weights(self, image, snapshot, index, legacy, bounds, size):
        ops = self.ops
        distance = legacy.distance
        left, top, right, bottom = bounds
        blank = ops.zero(image)
        original = ops.embed(blank, legacy.skin, (legacy.bounds[0]-left, legacy.bounds[1]-top))
        axes = ops.stage(image, 31, auxiliary=snapshot.artifact,
            args=(index, *size, 0, left, top), data=(*EYES[0], *EYES[1]), source_dependent=False)
        seeds = ops.stage(image, 32, auxiliary=original, field=axes, args=(distance, 0))
        distribution = self.distribution(image, seeds)
        guide = ops.stage(image, 40)

        def probability(value):
            raw = ops.stage(image, 18, auxiliary=value)
            filtered = ops.guided(ops.channel(raw), guide, max(1, round(.015*distance)), .02**2)
            return ops.stage(filtered, 19, auxiliary=raw)

        probability_image = blank
        if distribution is not None:
            probability_image = probability(distribution)
            seeds = ops.stage(image, 32, auxiliary=original, field=axes, extra=probability_image, args=(distance, 1))
            refined = self.distribution(image, seeds)
            if refined is not None:
                probability_image = probability(refined)

        def polygon(indices, extended=False):
            return ops.region(blank, 41 if extended else 32, field=snapshot.artifact,
                args=(index, len(indices), *size, left, top),
                data=(*indices, *EYES[0], *EYES[1]) if extended else indices)

        oval = polygon(OVAL)
        protected = blank
        for indices in (*EYES, *BROWS, LIPS, *NOSTRILS):
            protected = ops.stage(protected, 27, auxiliary=polygon(indices))
        protected = ops.dilate(protected, max(1, round(.02*distance)))
        protection = ops.inward(protected, .03*distance, invert=True)
        inside = ops.inward(oval, max(1., .25*distance), border=False, raw=True)
        outer = ops.inward(polygon(OVAL, True) if distribution is not None else oval, .20*distance)
        weights = ops.pack(axes, axes, outer, protection, channels=(0, 1, 0, 0))
        inputs = ops.pack(original, probability_image)
        return ops.stage(image, 20, auxiliary=inputs, field=weights, extra=inside,
                         args=(distance, int(distribution is not None)))

    def skin_delta(self, image, lab, weights, distance, params, index, bounds):
        ops = self.ops
        smooth, base_needed = bool(params["skin_smooth"]), bool(params["skin_smooth"] or params["skin_brighten"])

        # 两组滤波结果只取决于原图与皮肤权重，强度只在最后一步用到：按人脸保留，拖动磨皮、提亮、红润时不重算。
        # 用到哪几张取决于哪些滑块非零，所以键里带这两个开关
        def filtered():
            blank = ops.zero(image)
            y = ops.stage(image, 2)
            fine = base = mass = blank
            if base_needed:
                base = ops.guided(y, y, max(1, round(.08*distance)), .04**2)
            if smooth:
                fine = ops.guided(y, y, max(1, round(.015*distance)), .02**2)
                mass = ops.gaussian(ops.channel(weights), .04*distance)
            return (ops.pack(fine, base, mass),)

        def masked():
            skin = ops.channel(weights)
            chroma = ops.gaussian(ops.stage(lab, 17, auxiliary=skin), .04*distance) if smooth else ops.zero(image)
            return (ops.pack(chroma, chroma, skin, channels=(1, 2, 0, 0)),)

        fields, = self.retained("skin.fields", index, (bounds, smooth, base_needed), filtered)
        local, = self.retained("skin.local", index, (bounds, smooth), masked)
        return ops.stage(image, 13, auxiliary=lab, field=fields, extra=local,
            args=tuple(params[name]/100 for name in ("skin_smooth", "skin_brighten", "skin_rosy")))

    def spots(self, image, lab, y, skin, distance):
        ops = self.ops
        blank = ops.zero(image)
        fine = ops.guided(y, y, max(1, round(.01*distance)), .02**2)
        base = ops.guided(y, y, max(1, round(.04*distance)), .04**2)
        residual = ops.stage(fine, 255, auxiliary=base, args=(-1,))
        gradient = ops.stage(y, 29, args=(0,))
        selected = ops.stage(residual, 21, auxiliary=skin, kernel="beauty_spots")
        samples, count = selected, ops.count(ops.channel(selected, 3))
        if not count:
            return blank, 0
        median = ops.median(samples)
        deviations = ops.stage(samples, 4, auxiliary=median, kernel="beauty_stats")
        mad = ops.median(deviations)
        inputs = ops.pack(residual, skin, gradient)
        candidates = ops.stage(inputs, 18, auxiliary=median, field=mad, kernel="beauty_spots")
        labels = ops.stage(candidates, 0, kernel="beauty_spots")
        for _ in range(64):
            previous, labels = labels, ops.stage(labels, 1, kernel="beauty_spots")
        changed = ops.stage(labels, 2, auxiliary=previous, kernel="beauty_spots")
        changes = ops.count(changed)
        if changes:
            raise GpuExecutionError("beauty_target_limit", "Spot component labeling exceeds GPU submission limits")
        roots = ops.stage(labels, 3, kernel="beauty_spots")
        root_mask = ops.channel(roots, 3)
        count = ops.count(root_mask)
        if not count:
            return blank, 0
        if count > 256 or count*prod(image.resident.carrier.size) > 128_000_000:
            raise GpuExecutionError("beauty_target_limit", "Spot component statistics exceed GPU submission limits")
        components, _ = ops.selected(roots, root_mask)
        components = ops.components(labels, components, distance)
        components = ops.stage(components, 5, kernel="beauty_spots")
        coarse = ops.gaussian(lab, .04*distance)
        available = ops.stage(gradient, 16, auxiliary=skin, field=lab, extra=coarse, kernel="beauty_spots")
        available = ops.stage(available, 23, auxiliary=labels, kernel="beauty_spots")
        features = ops.stage(lab, 25, field=base)
        used, total, skipped = blank, blank, 0
        for index in range(count):
            descriptor = ops.stage(components, 17, size=(2, 2), args=(index,), kernel="beauty_spots")
            control = ops.control(descriptor)
            if not control[0, 1, 1]:
                continue
            left, top, right, bottom = (int(v) for v in control[0, 0])
            root = int(control[0, 1, 0])
            width, height = right-left, bottom-top
            cx, cy = control[1, 0, 1:3]
            image_width, image_height = image.resident.carrier.size
            search_left, search_top = max(0, floor(cx-.2*distance)), max(0, floor(cy-.2*distance))
            search_right = min(image_width, ceil(cx+.2*distance)+1)
            search_bottom = min(image_height, ceil(cy+.2*distance)+1)
            local = ops.stage(labels, 6, size=(width, height), args=(left, top, root), kernel="beauty_spots")
            pad = max(1, round(.03*distance))
            feather = max(2., .01*distance)
            outward = ops.inward(local, max(pad+1, feather), border=False, invert=True, raw=True)
            ring = ops.stage(outward, 10, auxiliary=available, args=(left, top, feather, pad), kernel="beauty_spots")
            means = ops.stage(features, 7, auxiliary=available, field=ring, size=(2, 1), args=(left, top), kernel="beauty_spots")
            availability = ops.stage(available, 19, auxiliary=used, kernel="beauty_spots")
            centers = ops.view(components, (1, index, 2, index+1))
            references = ops.assemble(means, [means, centers], [(0, 0), (2, 0)], (3, 1))
            scores = ops.stage(features, 8, auxiliary=availability, field=ring, extra=references,
                size=(search_right-search_left, search_bottom-search_top),
                args=(left, top, right, bottom, search_left, search_top, distance), kernel="beauty_spots",
                data=())
            donor = ops.stage(scores, 9, size=(2, 1), args=(width, height), kernel="beauty_spots")
            metadata = ops.control(donor)[0]
            if not metadata[1, 0]:
                skipped += 1
                continue
            dx, dy = (int(v) for v in metadata[0, :2])
            target = ops.view(image, (left, top, right, bottom))
            donor_image = ops.view(image, (dx, dy, dx+width, dy+height))
            blend = ops.stage(outward, 14, auxiliary=skin, args=(left, top, feather), kernel="beauty_spots")
            mass = ops.gaussian(ring, .02*distance)
            background = ops.gaussian(ops.stage(target, 17, auxiliary=ring), .02*distance)
            background = ops.stage(background, 10, auxiliary=mass)
            background = ops.stage(background, 20, kernel="beauty_spots")
            texture = ops.stage(donor_image, 255, auxiliary=ops.gaussian(donor_image, .01*distance), args=(-1,))
            masks = ops.pack(ring, blend)
            statistics = ops.stage(target, 11, auxiliary=background, field=texture, extra=masks, size=(2, 1), kernel="beauty_spots")
            valid = ops.channel(ops.view(statistics, (1, 0, 2, 1)), 3)
            if not ops.control(valid)[0, 0, 0]:
                skipped += 1
                continue
            repaired = ops.stage(ops.stage(target, 10, auxiliary=blend), 12, auxiliary=background,
                field=texture, extra=statistics, kernel="beauty_spots")
            total = ops.embed(total, repaired, (left, top), add=True)
            used = ops.stage(used, 13, auxiliary=donor, field=statistics, kernel="beauty_spots")
        return ops.stage(total, 15, kernel="beauty_spots"), skipped

    def independent(self, image, regions, params, index):
        ops = self.ops
        result, skipped = ops.zero(image), 0
        if params["sharpen"]:
            y = ops.stage(image, 2)
            fine = ops.guided(y, y, max(1, round(.015*regions.distance)), .02**2)
            delta = ops.stage(y, 15, auxiliary=fine, field=regions.features, args=(params["sharpen"]/100,))
            result = ops.stage(result, 8, auxiliary=delta, args=(1,))
        if params["teeth_whiten"]:
            delta = ops.stage(image, 14, auxiliary=ops.stage(image, 0), field=regions.mouth,
                              args=(params["teeth_whiten"]/100,))
            result = ops.stage(result, 8, auxiliary=delta, args=(1,))
        if params["spot_remove"]:
            def spots():
                lab = ops.stage(image, 0)
                return self.spots(image, lab, ops.stage(image, 2), ops.stage(lab, 4, auxiliary=regions.skin),
                                  regions.distance)
            delta, skipped = self.retained("spots", index, regions.bounds, spots)
            result = ops.stage(result, 8, auxiliary=delta, args=(params["spot_remove"]/100,))
        return result, skipped

    def eye_sites(self, candidate, masks):
        """各采样点蒙版选中像素的序号表，每点一行；不足 8 个像素的采样点是空行。

        只取决于蒙版，也就是关键点与皮肤权重，与美颜强度无关。"""
        ops = self.ops
        sites = [(mask, site) for mask in masks for site in range(9)]
        rows = {}

        def collect(number, selection, prefix, count):
            if count > ROW_SAMPLES:
                raise GpuExecutionError("beauty_target_limit", "Eye statistics exceed GPU submission limits")
            if count >= 8:
                rows[number] = ops.stage(candidate, 3, auxiliary=selection, field=prefix, size=(count, 1), args=(0,),
                                         kernel="beauty_stats")

        if candidate.resident.carrier.nbytes <= BATCH_BYTES:
            # 18 个点的像素数合并成一次读回
            selections = [mask(site) for mask, site in sites]
            prefixes = [ops.tally(candidate, selection) for selection in selections]
            for number, count in enumerate(ops.totals(prefixes)):
                collect(number, selections[number], prefixes[number], count)
        else:
            # 人脸区域很大时逐点读回：18 张蒙版同时持有要多占数百 MB 显存，逐点做则蒙版用完即放
            for number, (mask, site) in enumerate(sites):
                selection = mask(site)
                prefix = ops.tally(candidate, selection)
                collect(number, selection, prefix, ops.totals([prefix])[0])
        columns = max([1, *(row.resident.carrier.size[0] for row in rows.values())])
        return ops.assemble(candidate, list(rows.values()), [(0, number) for number in rows], (columns, len(sites)))

    def eyes(self, candidate, weights, snapshot, index, bounds, size, distance, strength):
        ops = self.ops
        lab = ops.stage(candidate, 0)
        base = ops.guided(lab, ops.stage(lab, 40), max(1, round(.08*distance)), .04**2)
        result = ops.zero(candidate)
        protected_candidate = ops.stage(candidate, 10, auxiliary=ops.channel(weights, 2))
        eyes = []
        for (upper, lower), (outer, inner) in zip(UNDER_EYE_CHAINS, ((33, 133), (263, 362))):
            arguments = (index, *size, outer, *bounds[:2], inner)
            geometry = ops.stage(candidate, 21, auxiliary=snapshot.artifact, args=arguments,
                                 data=(*upper, *lower), source_dependent=False)

            def measure(arguments=arguments):
                control = ops.stage(candidate, 36, auxiliary=snapshot.artifact, size=(1, 1), args=arguments,
                                    source_dependent=False)
                return control, float(ops.control(control)[0, 0, 0])
            width = self.retained("eye.width", (index, outer), arguments, measure, pixels=False)[1]

            def mask(site, geometry=geometry, width=width):
                return ops.stage(geometry, 22, auxiliary=weights, args=(distance, width, site))
            eyes.append((geometry, width, mask))
        # 两只眼 18 个采样点选中哪些像素只取决于蒙版：序号表按人脸保留，拖动美颜滑块时不再算蒙版、不再读回，
        # 每帧按表取样，18 个点的稳健统计按行一起算
        table, = self.retained("eye.sites", index, bounds,
                               lambda: (self.eye_sites(candidate, [mask for *_, mask in eyes]),))
        samples = ops.stage(base, 16, auxiliary=table, size=table.resident.carrier.size, kernel="beauty_stats")
        filtered, center = ops.robust(samples, (3, 2, 2), 2.5, rows=True)
        statistics = ops.stage(filtered, 10, auxiliary=center, size=(9, len(eyes)), args=(distance,),
                               kernel="beauty_stats")
        for number, (geometry, width, _) in enumerate(eyes):
            targets = ops.view(statistics, (0, number, 9, number+1))
            delta = ops.stage(base, 23, auxiliary=geometry, field=targets, extra=protected_candidate,
                              args=(distance, width, strength/100))
            result = ops.stage(result, 8, auxiliary=delta, args=(1,))
        return result

    def color(self, source, snapshot, index, regions, bounds, params):
        ops = self.ops
        image = ops.view(source, bounds)
        result = ops.zero(image)
        skipped = 0
        if any(params[key] for key in ("sharpen", "teeth_whiten", "spot_remove")):
            previous, skipped = self.independent(ops.view(source, regions.bounds), regions, params, index)
            result = ops.embed(result, previous, (regions.bounds[0]-bounds[0], regions.bounds[1]-bounds[1]))
        if any(params[key] for key in ("skin_smooth", "skin_brighten", "skin_rosy", "eye_bag_remove")):
            lab = ops.stage(image, 0)
            size = source.pixels.shape[1::-1]
            weights, = self.retained("skin.weights", index, bounds,
                                     lambda: (self.skin_weights(lab, snapshot, index, regions, bounds, size),))
            if any(params[key] for key in ("skin_smooth", "skin_brighten", "skin_rosy")):
                delta = self.skin_delta(image, lab, weights, regions.distance, params, index, bounds)
                result = ops.stage(result, 8, auxiliary=delta, args=(1,))
            if params["eye_bag_remove"]:
                candidate = ops.stage(image, 12, auxiliary=result)
                delta = self.eyes(candidate, weights, snapshot, index, bounds, size, regions.distance, params["eye_bag_remove"])
                result = ops.stage(image, 37, auxiliary=result, field=delta, extra=candidate)
        return result, skipped

    def warp(self, source, snapshot, index, regions, params):
        ops = self.ops
        left, top, right, bottom = regions.bounds
        width, height = right-left, bottom-top
        ratio = min(1., 512/max(width, height))
        size = max(1, round(width*ratio)), max(1, round(height*ratio))
        field = ops.stage(source, 34, auxiliary=snapshot.artifact, size=size,
            args=(index, *source.pixels.shape[1::-1], regions.distance, left, top, width, height,
                  *(params[key]/100 for key in WARP_KEYS)), data=(*JAW[0], *JAW[1], *EYES[0], *EYES[1]),
            source_dependent=False)
        field = self.backend.operate(field, "beauty.field.resize", (width, height), (width, height),
            lambda device, abort: device.image_op(field.resident.carrier, field.resident.carrier, 7, [], width, height, abort), self.cancelled)
        return ops.stage(field, 6, auxiliary=regions.support)

    def execute(self, source, params, snapshot, *, node_id=""):
        if not any(value for key, value in params.items() if key not in BEAUTY_MODEL_FIELDS):
            return source
        if snapshot.reference_size != source.orig_size:
            raise GpuExecutionError("beauty_input", "Beauty snapshot reference size mismatch")
        ops = self.ops
        size = source.pixels.shape[1::-1]
        image = source.gpu_image
        self.slot = image.source_path, "beauty", node_id, size
        self.landmarks = snapshot.artifact.resident.identity
        self.pixels = self.landmarks, image.resident.identity, tuple(patch.identity for patch in image.patches)

        def read_records():
            control = snapshot.session.op(snapshot.artifact, 17, (2, snapshot.artifact.resident.carrier.size[1]),
                                          self.cancelled)
            return control, ops.control(control)
        records = self.retained("records", 0, (), read_records, pixels=False)[1]
        present = [index for index, row in enumerate(records) if row[0, 1]]
        ready = [index for index in present if records[index, 1, 0] == 1]
        skipped = len(present)-len(ready)
        if not present and records[0, 1, 1]:
            skipped = 1
        status = "ready" if ready else {2: "face_too_small", 3: "insufficient_landmarks"}.get(
            int(records[present[0], 1, 0]) if present else 3 if skipped else 0, "no_face")
        effect = BeautyEffectStatus(node_id, status, len(ready), skipped)
        if not ready:
            return replace(source, effect_status=(*source.effect_status, effect))
        has_color, has_warp = any(params[key] for key in COLOR_KEYS), any(params[key] for key in WARP_KEYS)
        expanded = any(params[key] for key in ("skin_smooth", "skin_brighten", "skin_rosy", "eye_bag_remove"))
        required = set()
        if expanded or params["spot_remove"]:
            required.add("skin")
        if params["sharpen"]:
            required.add("features")
        if params["teeth_whiten"]:
            required.add("mouth")
        if has_warp:
            required.add("support")
        entries = []
        for index in ready:
            regions = self.regions(snapshot, index, size, required)
            bounds = regions.bounds
            if has_color and expanded:
                def expand(index=index):
                    metadata = snapshot.session.op(snapshot.artifact, 16, (2, 1), self.cancelled,
                        args=(index, .65, 0, 0, *size), data=(*EYES[0], *EYES[1]))
                    return metadata, tuple(int(v) for v in ops.control(metadata)[0, 0])
                bounds = self.retained("bounds", index, (), expand, pixels=False)[1]
            if prod((bounds[2]-bounds[0], bounds[3]-bounds[1])) > 4_000_000:
                raise GpuExecutionError("beauty_target_limit", "Face statistics target exceeds GPU submission limits")
            entries.append((index, regions, bounds))
        bounds = (min(item[2][0] for item in entries), min(item[2][1] for item in entries),
                  max(item[2][2] for item in entries), max(item[2][3] for item in entries))
        left, top, right, bottom = bounds
        composition = ops.view(source, bounds)
        colors = ops.zero(composition) if has_color else None
        spots_skipped = 0
        for index, regions, color_bounds in entries:
            if has_color:
                delta, missed = self.retained("color", index, (color_bounds, *(params[key] for key in COLOR_KEYS)),
                    lambda: self.color(source, snapshot, index, regions, color_bounds, params))
                colors = ops.embed(colors, delta, (color_bounds[0]-left, color_bounds[1]-top), add=True)
                spots_skipped += missed
        result = source
        geometry = GpuGeometry(self.backend)
        if has_color:
            composed = ops.stage(composition, 12, auxiliary=colors, args=(1,))
            result = geometry.with_patch(result, composed, bounds, (composed.resident.identity,), has_alpha=False)
        if has_warp:
            def displacement():
                fields, weights = ops.zero(composition), ops.zero(composition)
                for index, regions, _ in entries:
                    offset = regions.bounds[0]-left, regions.bounds[1]-top
                    fields = ops.embed(fields, self.warp(source, snapshot, index, regions, params), offset, add=True)
                    weights = ops.embed(weights, regions.support, offset, add=True)
                field = ops.stage(fields, 28, auxiliary=weights)
                blocks = ceil((right-left)*(bottom-top)/1024)
                extents = ops.stage(field, 38, size=(min(blocks, 32768), ceil(blocks/32768)), args=(left, top, *size))
                extent = ops.stage(extents, 39, size=(1, 1), args=size)
                return field, tuple(int(v) for v in ops.control(extent)[0, 0])
            # 位移场和它的取样范围只取决于关键点与形变参数：拖动其他美颜滑块时沿用，不再重算、不再读回
            field, sample_bounds = self.retained("warp", 0, (
                bounds, tuple((index, regions.bounds) for index, regions, _ in entries),
                *(params[key] for key in WARP_KEYS)), displacement, pixels=False)
            image = self.backend.resolve(result.gpu_image, self.cancelled, sample_bounds)
            warped = self.backend.operate(image, "beauty.remap", (field.resident.identity, bounds, sample_bounds), (right-left, bottom-top),
                lambda device, abort: device.image_op(image.resident.carrier, field.resident.carrier, 15,
                    [left, top, *sample_bounds[:2], *size], right-left, bottom-top, abort), self.cancelled)
            result = geometry.with_patch(result, warped, bounds, (warped.resident.identity,), has_alpha=source.alpha is not None)
        return replace(result, effect_status=(*source.effect_status, replace(effect, spots_skipped=spots_skipped)))
