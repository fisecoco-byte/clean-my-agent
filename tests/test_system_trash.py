from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import system_trash


class SystemTrash(unittest.TestCase):
    @patch('clean_my_agent.system_trash.available',return_value=True)
    def test_native_trash_preserves_path_arguments_and_checks_identity(self,available):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)
            source=parent/'空格 " quote\n$(test)'; source.mkdir()
            destination=parent/'trash\n'
            def move(command,**kwargs):
                self.assertEqual(command[-1],str(source))
                self.assertNotIn(str(source),command[-2])
                self.assertNotIn('shell',kwargs)
                source.rename(destination)
                return subprocess.CompletedProcess(command,0,bytes(str(destination)+'\n','utf-8'),b'')
            with patch.object(system_trash.subprocess,'run',side_effect=move):
                self.assertEqual(system_trash.move(source),destination)

    @patch('clean_my_agent.system_trash.available',return_value=True)
    def test_native_failure_or_unverified_destination_keeps_original(self,available):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'repo'; source.mkdir()
            for result in (subprocess.CompletedProcess([],1,b'',b'access denied'),
                           subprocess.CompletedProcess([],0,b'/unverified/location\n',b'')):
                with self.subTest(result=result),patch.object(system_trash.subprocess,'run',return_value=result) as run:
                    with self.assertRaises(ValueError):system_trash.move(source)
                    self.assertTrue(source.exists())
                    run.assert_called_once()

    @patch('clean_my_agent.system_trash.available',return_value=False)
    def test_unsupported_system_never_runs_command(self,available):
        with patch.object(system_trash.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'仅支持 macOS'):
                system_trash.move(Path('/unused'))
            run.assert_not_called()
