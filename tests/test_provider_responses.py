"""Exercise real SDK parsing against local HTTP responses, without API calls."""
import json
import httpx2
import pytest
from openai import OpenAI
from prose_to_video.pipeline.providers import AIResponseError, AISettings, OpenAIProvider


def provider_with_response(body):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=body)
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.settings = AISettings()
    provider.client = OpenAI(api_key='test-only', http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
    return provider, requests


def response(text, *, status='completed', reason=None, refusal=False):
    return {'id': 'resp_test', 'object': 'response', 'created_at': 0, 'model': 'test',
            'status': status, 'incomplete_details': {'reason': reason} if reason else None,
            'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': status,
                        'content': [{'type': 'refusal', 'refusal': 'private refusal'}] if refusal else
                                   [{'type': 'output_text', 'text': text, 'annotations': []}]}],
            'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': []}


@pytest.mark.parametrize('text', ['{"characters": [{"name": "Mara"', '{"characters": []}'])
def test_incomplete_checked_before_sdk_parsing(text):
    provider, requests = provider_with_response(response(text, status='incomplete', reason='max_output_tokens'))
    with pytest.raises(AIResponseError, match='output token limit') as caught:
        provider.detect_characters('{}')
    assert caught.value.retry_smaller
    assert len(requests) == 1
    assert requests[0]['text']['format']['strict'] is True
    provider.client.close()


def test_complete_response_and_null_unknowns():
    provider, requests = provider_with_response(response(json.dumps({'characters': [{
        'name': 'Mara', 'paragraph_ids': [1, 2], 'aliases': None, 'appearance_description': None}]})))
    character = provider.detect_characters('{}').characters[0]
    assert character.appearance_description == ''
    assert character.aliases == []
    assert len(requests) == 1
    provider.client.close()


def test_invalid_field_diagnostic_does_not_disclose_values():
    provider, _ = provider_with_response(response(json.dumps({'characters': [{
        'name': 'Mara', 'paragraph_ids': ['PRIVATE PROSE'], 'SECRET EXTRA FIELD': 'private'}]})))
    with pytest.raises(AIResponseError) as caught:
        provider.detect_characters('{}')
    message = str(caught.value)
    assert 'characters.0.paragraph_ids.0: int_parsing' in message
    assert 'PRIVATE' not in message and 'SECRET' not in message
    assert not caught.value.retry_smaller
    provider.client.close()


@pytest.mark.parametrize('body', [response('', refusal=True), response('', status='incomplete', reason='content_filter')])
def test_refusal_and_other_incomplete_output_are_not_retried(body):
    provider, _ = provider_with_response(body)
    with pytest.raises(AIResponseError) as caught:
        provider.detect_characters('{}')
    assert not caught.value.retry_smaller
    provider.client.close()


def test_character_generation_uses_dedicated_pronoun_request(tmp_path):
    from prose_to_video.pipeline.character_detection import detect_characters
    from prose_to_video.storage.project_files import create_project
    from prose_to_video.storage.visual_library import VisualLibrary
    requests = []
    def respond(request):
        body = json.loads(request.content); requests.append(body)
        if len(requests) == 1:
            output = {'characters': [{'name': 'Mara', 'paragraph_ids': [1]}]}
        else:
            assert body['text']['format']['name'] == 'CharacterMentions'
            assert 'Mara participates in [1,2,3]' in body['instructions']
            payload = json.loads(body['input'])
            assert payload['known_characters'][0]['name'] == 'Mara'
            assert len(payload['paragraphs']) == 3
            output = {'characters': [{'name': 'Mara', 'paragraph_ids': [1, 2, 3]}]}
        return httpx2.Response(200, json=response(json.dumps(output)))
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.settings = AISettings()
    provider.client = OpenAI(api_key='test-only', http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
    library = VisualLibrary(create_project(tmp_path, 'SDKPronouns').root)
    result = detect_characters('Mara enters.\nShe sits.\nHer hands shake.', library, provider, provider.settings)
    assert result[0].name == 'Mara'
    assert result[0].character_paragraphs == [1, 2, 3]
    assert len(requests) == 2
    provider.client.close()
