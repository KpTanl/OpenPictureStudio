from .tools import ToolReference


IMAGE_PLUGIN_API = 1
IMAGE_PLUGIN_RESOURCE = "image-plugin-api-1"


def workflow_plugin_dependencies(graphs):
    dependencies = {}
    for graph in graphs:
        for node in graph.nodes:
            if node.node_type != "tool":
                continue
            reference = ToolReference.from_dict(node.params)
            for role, digest in reference.resources:
                if role == IMAGE_PLUGIN_RESOURCE:
                    entry = {"tool_id": reference.tool_id, "params_version": reference.params_version,
                             "implementation_id": reference.implementation_id,
                             "implementation_version": reference.implementation_version, "api_version": IMAGE_PLUGIN_API}
                    if entry not in dependencies.setdefault(digest, []):
                        dependencies[digest].append(entry)
    return {digest: sorted(entries, key=lambda entry: (entry["tool_id"], entry["implementation_version"]))
            for digest, entries in sorted(dependencies.items())}
