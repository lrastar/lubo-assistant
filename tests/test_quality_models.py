import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lubo.quality_models import MODELS, download_model


class OfflineModelTests(unittest.TestCase):
    def test_verified_complete_cache_never_contacts_network(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, revision = MODELS['asr']
            directory = root / repo.split('/')[-1]
            directory.mkdir()
            required = ['config.json', 'processor_config.json', 'tokenizer.json', 'tokenizer_config.json',
                        'chat_template.jinja', 'model.safetensors']
            for name in required:
                (directory / name).write_bytes(b'fixture')
            (directory / 'lubo-model.json').write_text(json.dumps(
                {'repo': repo, 'revision': revision, 'verified': True, 'required_files': required}), encoding='utf-8')
            with patch('lubo.quality_models.urllib.request.urlopen', side_effect=AssertionError('offline')) as network:
                download_model('asr', root)
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
