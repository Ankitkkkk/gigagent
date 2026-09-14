"""Queued prompts always carry the registered name, including after renames."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wrapper


class StopWatcher(BaseException):
    pass


class IdentityPromptTests(unittest.TestCase):
    def test_identity_startup_does_not_swallow_coalesced_mentions(self):
        for reverse in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                queue = Path(directory) / 'queue.jsonl'
                entries = [dict(channel='ws-project', prompt='Identity setup; wait for requests.'),
                           dict(channel='ws-project', message='@reviewer help')]
                if reverse:
                    entries.reverse()
                queue.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
                prompts = []
                def inject(prompt):
                    prompts.append(prompt)
                    raise StopWatcher
                with patch.object(wrapper.time, 'sleep'), patch.object(wrapper, '_fetch_role', return_value=''), \
                        patch.object(wrapper, '_fetch_active_rules', return_value=None):
                    with self.assertRaises(StopWatcher):
                        wrapper._queue_watcher(lambda: ('reviewer', queue), inject)
                self.assertIn('Identity setup', prompts[0])
                self.assertIn('read #ws-project', prompts[0])
                self.assertIn('take appropriate action and respond', prompts[0])

    def test_prompts_use_current_assigned_name_for_single_and_multiple_instances(self):
        for multi in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                queue = Path(directory) / 'queue.jsonl'
                current = ['user-provided-agent']
                prompts = []
                def enqueue():
                    queue.write_text(json.dumps(dict(channel='ws-project', prompt='Read the latest request.')) + '\n')
                def inject(prompt):
                    prompts.append(prompt)
                    if len(prompts) == 2:
                        raise StopWatcher
                    current[0] = 'renamed-reviewer'
                    enqueue()
                enqueue()
                with patch.object(wrapper.time, 'sleep'), patch.object(wrapper, '_fetch_role', return_value=''), \
                        patch.object(wrapper, '_fetch_active_rules', return_value=None):
                    with self.assertRaises(StopWatcher):
                        wrapper._queue_watcher(lambda: (current[0], queue), inject,
                                               is_multi_instance=multi, agent_name='codex')
                for prompt, name in zip(prompts, ('user-provided-agent', 'renamed-reviewer')):
                    self.assertIn(name, prompt)
                    self.assertIn('ws-project', prompt)
                    self.assertIn('Read the latest request.', prompt)
                    self.assertNotIn('reclaim your previous identity', prompt)
                    self.assertNotIn('give you a name', prompt)
