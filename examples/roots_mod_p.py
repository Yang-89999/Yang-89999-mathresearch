"""Smallest possible experiment: count roots of x^2 - a over F_p.

Runs under plain Python or Sage. Prints a RESULT line the sandbox parses.
"""
import json

p = 101
counts = {}
for a in range(p):
    roots = [x for x in range(p) if (x * x - a) % p == 0]
    counts[len(roots)] = counts.get(len(roots), 0) + 1

print("root-count distribution over F_%d:" % p, counts)
print("RESULT:", json.dumps({"p": p, "distribution": counts}))
