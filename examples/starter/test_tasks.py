import unittest

from tasks import list_tasks


class TaskListTest(unittest.TestCase):
    def test_returns_titles_in_order(self):
        self.assertEqual(list_tasks([
            {"title": "Write spec", "done": False},
            {"title": "Review change", "done": True},
        ]), ["Write spec", "Review change"])

    def test_empty_list(self):
        self.assertEqual(list_tasks([]), [])


if __name__ == "__main__":
    unittest.main()
