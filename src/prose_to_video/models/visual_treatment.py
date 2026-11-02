"""Shared storyboard treatments and concrete image instructions."""
from typing import Literal, get_args

VisualTreatment = Literal["literal", "establishing", "illustrative", "montage", "screen", "symbolic",
                          "concept_map", "blueprint", "cad_visualization", "product_showcase"]
VISUAL_TREATMENTS = get_args(VisualTreatment)
TECHNICAL_TREATMENTS = {
    "concept_map": "Systems concept map with clear nodes, connections and directional flows; label only source-supported relationships.",
    "blueprint": "Technical drafting illustration with orthographic or sectional views and engineering callouts; do not invent measurements or specifications.",
    "cad_visualization": "CAD-style industrial design visualization with isometric or exploded geometry and material emphasis; unsupported construction details are illustrative, not engineering facts.",
    "product_showcase": "Hero product illustration with clean composition, clear silhouette and material detail; preserve the approved artistic medium.",
}
TECHNICAL_PLANNING = (
    "For technical exposition choose concept_map for systems and flows, blueprint for construction "
    "and sections, cad_visualization for geometry and prototypes, or product_showcase for a device hero view "
    "when this communicates the passage better than a literal scene. Choose semantically, not from isolated "
    "technology words. Preserve screen/viewpoint context. Do not invent specifications, labels or measurements. "
)
