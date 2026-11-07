"""Resolve scene details to the catalogs shown in Places and Characters."""
from prose_to_video.models.scene import source_hash
from prose_to_video.pipeline.character_detection import character_paragraphs, listed_characters
from prose_to_video.pipeline.place_detection import listed_places


def scene_references(library, text, scenes):
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    paragraphs = []
    cursor = 0
    for paragraph in character_paragraphs(text):
        start = normalized.index(paragraph, cursor)
        cursor = start + len(paragraph)
        paragraphs.append((start, cursor))
    digest = source_hash(text)
    characters = listed_characters(library)
    places = [(place, set(library.scenes_for(place.identifier)))
              for place in listed_places(library)]
    references = {}
    for scene in scenes:
        start = len(text[:scene.start].replace('\r\n', '\n').replace('\r', '\n'))
        end = len(text[:scene.end].replace('\r\n', '\n').replace('\r', '\n'))
        appearances = {i for i, (a, b) in enumerate(paragraphs, 1) if a < end and b > start}
        references[scene.identifier] = (
            [place.name for place, links in places if scene.identifier in links],
            [character.name for character in characters
             if character.source_hash == digest and appearances.intersection(character.character_paragraphs)],
        )
    return references
