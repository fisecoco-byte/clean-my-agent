import unittest

from clean_my_agent.inventory import BUCKETS
from clean_my_agent.pie import MARKS, pie_lines
from clean_my_agent.terminal import cells


class PieTests(unittest.TestCase):
    def render(self, counts, width):
        return [''.join(s for s, _ in row) for row in pie_lines(dict(zip(BUCKETS, counts)), width)]

    def test_halves_with_empty_buckets_and_exact_legend(self):
        chart = self.render([0, 4, 0, 4, 0], 80)
        for row in chart:
            left, right = row[:9].strip(), row[9:18].strip()
            self.assertEqual(set(left), {MARKS[3]})
            self.assertEqual(set(right), {MARKS[1]})
        self.assertEqual(sum('50.0%' in row for row in chart), 2)
        self.assertEqual(sum('0 个' in row for row in chart), 3)

    def test_empty_single_tiny_slices_and_stacked_layout(self):
        self.assertEqual(self.render([0]*5, 30), ['暂无会话'])
        rows = self.render([0,0,0,0,1], 30)
        self.assertEqual(set(''.join(rows[:8]).replace(' ','')), {MARKS[-1]})
        self.assertIn('100%', rows[-1])
        for row in self.render([1,0,10000,0,0], 32):
            self.assertLessEqual(cells(row), 32)
        self.assertIn('<0.1%', '\n'.join(self.render([1,0,10000,0,0], 32)))
