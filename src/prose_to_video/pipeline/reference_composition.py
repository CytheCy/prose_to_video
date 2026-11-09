"""Composition guidance shared by character reference generation paths."""

CHARACTER_REFERENCE_SUBJECTS = (
    'A character entry may represent an individual or a group of people. '
    'Determine the number of subjects from the character name and appearance description. '
    'When a number is stated in words or digits, show exactly that many people together in this image. '
    'Preserve groups as groups; do not reduce them to a single representative person. '
    'For an individual character, show only that individual. '
    'For a group without a stated count, show multiple people consistent with its description. '
    'Arrange group members side by side so every face and full body is clearly visible, '
    'with distinct silhouettes and no member hidden behind another. '
    'Include only the intended subjects; no unrelated people. '
)

CHARACTER_REFERENCE_PEOPLE_ONLY = (
    '\nReference content restriction: include only the people and their worn clothing '
    'on a blank solid white background. Keep all hands empty. '
    'Do not include props, handheld or carried objects, bags, luggage, tools, weapons, '
    'furniture, vehicles, animals, scenery or other scene objects. '
    'Ignore any such objects or scene actions mentioned in the character name, '
    'appearance description or visual style; those details must not appear in this reference. '
    'Preserve the intended number of people and their physical appearance.'
)
