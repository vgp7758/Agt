from workflow_xml import canvas_to_xml, xml_to_canvas
import re

item_schema = [
    {"name": "kind", "type": "string"},
    {"name": "turn_idx", "type": "integer"},
    {"name": "call_id", "type": "string"},
    {"name": "confidence", "type": "number"},
]
output_schema = [
    {"name": "selected", "type": "list", "schema": item_schema},
    {"name": "summary", "type": "string"},
]
outputs = [{"name": "output", "type": "object", "schema": output_schema}]

canvas = {
    "nodes": [{
        "id": "160001",
        "type": "3",
        "data": {
            "inputs": {"inputParameters": [], "llmParam": []},
            "outputs": outputs,
            "nodeMeta": {"title": "llm"},
        },
    }],
    "edges": [],
}

xml = canvas_to_xml(canvas, {"name": "test"})
m = re.search(r'<out name="output".*?(?:</out>|/>)', xml, re.DOTALL)
print("XML:", m.group() if m else "NOT FOUND")

c2 = xml_to_canvas(xml)
sel = c2["nodes"][0]["data"]["outputs"][0]["schema"][0]
print("selected type:", sel["type"], "has schema:", "schema" in sel)
if "schema" in sel:
    print("items:", [f["name"] for f in sel["schema"]])
