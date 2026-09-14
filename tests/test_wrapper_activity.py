"""Activity must follow work, not Codex's idle composer particles."""
import subprocess
import unittest
from unittest.mock import patch

from wrapper_unix import get_activity_checker, waiting_for_input


class InputPromptTests(unittest.TestCase):
    def test_common_confirmation_prompts(self):
        for pane in (
            'Would you like to run this command?\n› 1. Yes, proceed (y)\n  2. No (esc)\nPress enter to confirm or esc to cancel',
            'Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel',
            'Trust this folder?\nEnter to select · Esc to cancel',
            'Continue? [y/N]',
            '\x1b[32mAllow access? (y/n)\x1b[0m',
        ):
            with self.subTest(pane=pane):
                self.assertTrue(waiting_for_input(pane.encode()))

    def test_idle_work_and_old_prompt_are_not_waiting(self):
        for pane in (
            screen(), b'Working (esc to interrupt)',
            b'We should add approval detection.',
            b'Press enter to confirm or esc to cancel\nDone.\n\n> Ask anything',
            b'Continue? [y/N]\nYes\nProcessing files...',
            b'Example: Continue? [y/N]\n' + b'ordinary output\n' * 20,
        ):
            with self.subTest(pane=pane):
                self.assertFalse(waiting_for_input(pane))

    def test_capture_tracks_prompt_and_clears_on_resolution_or_failure(self):
        checker = get_activity_checker('test')
        prompt = b'Press enter to confirm or esc to cancel'
        frames = [subprocess.CompletedProcess([], 0, stdout=value)
                  for value in (prompt, prompt, screen(), prompt)]
        frames += [subprocess.CompletedProcess([], 1, stdout=prompt), OSError('gone')]
        with patch('wrapper_unix.subprocess.run', side_effect=frames):
            for expected in (True, True, False, True, False, False):
                self.assertIsInstance(checker(), bool)
                self.assertEqual(checker.waiting_for_input, expected)


def screen(body="Finished.", frame=0, prompt="Ask Codex to do anything"):
    particles = [
        ["⠁     ⠄", f"› {prompt} ⢀", "   ⡀  ⠐"],
        ["  ⠈      ⠂", f"›⠁{prompt}    ⠄", " ⠠    ⢀"],
    ][frame]
    return (body + "\n\n" + "\n".join(particles)
            + "\n  gpt-6-astra high · ~/dev\n").encode()


class ActivityTests(unittest.TestCase):
    def activity(self, frames, provider="codex"):
        checker = get_activity_checker("test", provider=provider)
        results = [subprocess.CompletedProcess([], 0, stdout=frame) for frame in frames]
        with patch("wrapper_unix.subprocess.run", side_effect=results):
            return [checker() for _ in frames]

    def test_idle_particles_do_not_count_as_work(self):
        self.assertEqual(self.activity([screen(), screen(frame=1), screen()]),
                         [False, False, False])

    def test_work_still_counts_then_returns_idle(self):
        self.assertEqual(self.activity([
            screen(), screen("Working (1s • esc to interrupt)", 1),
            screen("Working (2s • esc to interrupt)"), screen("Done.", 1),
            screen("Done."),
        ]), [False, True, True, True, False])

    def test_real_braille_output_and_spinners_are_preserved(self):
        self.assertEqual(self.activity([
            screen("Output: ⠁"), screen("Output: ⠂", 1),
            screen("⠋ Working"), screen("⠙ Working", 1),
        ]), [False, True, True, True])

    def test_typing_still_counts(self):
        self.assertEqual(self.activity([screen(), screen(frame=1, prompt="hello")]),
                         [False, True])

    def test_other_providers_are_unchanged(self):
        self.assertEqual(self.activity([screen(), screen(frame=1)], provider="claude"),
                         [False, True])

    def test_no_prompt_does_not_filter_output(self):
        self.assertEqual(self.activity(["⠁\n".encode(), "⠂\n".encode()]),
                         [False, True])

    def test_trigger_reports_activity_even_on_unchanged_screen(self):
        trigger = [False]
        checker = get_activity_checker("test", trigger_flag=trigger, provider="codex")
        result = subprocess.CompletedProcess([], 0, stdout=screen())
        with patch("wrapper_unix.subprocess.run", return_value=result):
            self.assertFalse(checker())
            trigger[0] = True
            self.assertTrue(checker())
            self.assertFalse(trigger[0])
            self.assertFalse(checker())
