"""Question bank for the PracticeGeneratorAgent.

Curated deterministic MCQs (with a known correct answer, explanations and
misconceptions) plus generated conceptual tasks derived from each skill's
learning objectives. Every question is real and markable; nothing is a stub.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List

CURATED: List[Dict[str, Any]] = [
    {
        "skill": "DSA", "topic": "Arrays", "difficulty": "EASY",
        "question": "What is the time complexity of accessing an element by index in an array?",
        "options": ["O(1)", "O(log n)", "O(n)", "O(n log n)"],
        "correct_index": 0,
        "explanation": "Arrays store elements contiguously, so an index maps directly to a memory offset.",
        "misconceptions": ["Searching by value is O(1) — it is O(n)", "Index access needs a scan"],
        "hints": ["Think about how the memory address is computed from the index."],
        "estimated_minutes": 5,
    },
    {
        "skill": "DSA", "topic": "Binary search", "difficulty": "MEDIUM",
        "question": "Binary search on a sorted array of n elements runs in:",
        "options": ["O(n)", "O(log n)", "O(n log n)", "O(1)"],
        "correct_index": 1,
        "explanation": "Each comparison halves the search space, giving log2(n) steps.",
        "misconceptions": ["It works on unsorted arrays", "Worst case is linear"],
        "hints": ["How much of the array remains after each comparison?"],
        "estimated_minutes": 5,
    },
    {
        "skill": "DSA", "topic": "Recursion", "difficulty": "MEDIUM",
        "question": "What must every correct recursive function have?",
        "options": [
            "A base case and a recursive step that moves toward it",
            "A loop inside the function",
            "Global variables to track depth",
            "At least two recursive calls",
        ],
        "correct_index": 0,
        "explanation": "Without a base case reached by progressively smaller inputs, recursion never terminates.",
        "misconceptions": ["Recursion always needs two calls", "Base case is optional for small inputs"],
        "hints": ["What stops the function from calling itself forever?"],
        "estimated_minutes": 6,
    },
    {
        "skill": "DSA", "topic": "Hash tables", "difficulty": "MEDIUM",
        "question": "The average time complexity of a hash table lookup is:",
        "options": ["O(log n)", "O(1)", "O(n)", "O(n log n)"],
        "correct_index": 1,
        "explanation": "A good hash function distributes keys so lookups land directly on the bucket.",
        "misconceptions": ["Hash lookups are always worst-case O(1)", "Collisions are impossible"],
        "hints": ["What does the hash function compute?"],
        "estimated_minutes": 5,
    },
    {
        "skill": "DSA", "topic": "Big-O", "difficulty": "EASY",
        "question": "Which growth rate is the fastest as n becomes very large?",
        "options": ["O(n log n)", "O(n^2)", "O(log n)", "O(n)"],
        "correct_index": 1,
        "explanation": "Quadratic growth dominates linear and logarithmic growth asymptotically.",
        "misconceptions": ["Constants matter asymptotically", "log n grows faster than n"],
        "hints": ["Compare n=1,000,000 across each option."],
        "estimated_minutes": 5,
    },
    {
        "skill": "SQL", "topic": "joins", "difficulty": "EASY",
        "question": "Which JOIN returns only rows that have matching values in both tables?",
        "options": ["LEFT JOIN", "INNER JOIN", "FULL OUTER JOIN", "CROSS JOIN"],
        "correct_index": 1,
        "explanation": "INNER JOIN keeps rows where the join predicate matches on both sides.",
        "misconceptions": ["LEFT JOIN keeps only matches", "CROSS JOIN filters on keys"],
        "hints": ["Which join type intersects the two tables?"],
        "estimated_minutes": 5,
    },
    {
        "skill": "SQL", "topic": "aggregation", "difficulty": "MEDIUM",
        "question": "Which clause filters groups produced by GROUP BY?",
        "options": ["WHERE", "HAVING", "ORDER BY", "LIMIT"],
        "correct_index": 1,
        "explanation": "WHERE filters rows before grouping; HAVING filters groups after aggregation.",
        "misconceptions": ["WHERE can filter on aggregates", "ORDER BY changes grouping"],
        "hints": ["It runs after aggregation, unlike WHERE."],
        "estimated_minutes": 6,
    },
    {
        "skill": "Python", "topic": "data structures", "difficulty": "EASY",
        "question": "Which Python collection is immutable?",
        "options": ["list", "dict", "tuple", "set"],
        "correct_index": 2,
        "explanation": "Tuples cannot be modified after creation; lists, dicts and sets are mutable.",
        "misconceptions": ["Lists are immutable", "Sets preserve insertion order"],
        "hints": ["You cannot append to it or reassign an element."],
        "estimated_minutes": 4,
    },
    {
        "skill": "Python", "topic": "generators", "difficulty": "MEDIUM",
        "question": "What does the 'yield' keyword do in a Python function?",
        "options": [
            "Terminates the function and returns a value",
            "Turns the function into a generator that lazily produces values",
            "Raises an exception",
            "Runs the function in a new thread",
        ],
        "correct_index": 1,
        "explanation": "yield suspends the function, producing one value at a time without building a full list in memory.",
        "misconceptions": ["yield behaves exactly like return", "Generators are eager"],
        "hints": ["How do you stream values without storing them all?"],
        "estimated_minutes": 6,
    },
    {
        "skill": "OOP", "topic": "encapsulation", "difficulty": "EASY",
        "question": "Encapsulation primarily means:",
        "options": [
            "Hiding internal state behind a controlled interface",
            "Inheriting behaviour from a parent class",
            "Creating many objects from one class",
            "Overloading operators",
        ],
        "correct_index": 0,
        "explanation": "Encapsulation bundles data with the methods that operate on it and restricts direct access.",
        "misconceptions": ["Encapsulation is the same as inheritance", "Private fields are about security only"],
        "hints": ["Think about getters/setters and private state."],
        "estimated_minutes": 5,
    },
    {
        "skill": "DBMS", "topic": "transactions", "difficulty": "MEDIUM",
        "question": "In ACID, 'Isolation' guarantees that:",
        "options": [
            "Committed changes survive crashes",
            "Concurrent transactions do not see each other's intermediate state",
            "Transactions are all-or-nothing",
            "Data always satisfies constraints",
        ],
        "correct_index": 1,
        "explanation": "Isolation levels control how much in-progress work of one transaction is visible to others.",
        "misconceptions": ["Isolation means durability", "Atomicity covers concurrency"],
        "hints": ["Which property is about concurrency?"],
        "estimated_minutes": 6,
    },
    {
        "skill": "OS", "topic": "processes", "difficulty": "MEDIUM",
        "question": "The key difference between a process and a thread is:",
        "options": [
            "Threads have their own address space",
            "Processes share memory by default",
            "Threads within a process share its address space",
            "Processes cannot run concurrently",
        ],
        "correct_index": 2,
        "explanation": "Threads of one process share the process address space; processes are isolated from each other.",
        "misconceptions": ["Processes share heaps by default", "Threads are always faster to schedule than processes"],
        "hints": ["What does a context switch between threads avoid?"],
        "estimated_minutes": 6,
    },
    {
        "skill": "CN", "topic": "HTTP", "difficulty": "EASY",
        "question": "HTTP is built on top of which transport protocol by default?",
        "options": ["UDP", "TCP", "ICMP", "ARP"],
        "correct_index": 1,
        "explanation": "HTTP/1.1 and HTTP/2 run over reliable, ordered TCP streams (HTTP/3 uses QUIC/UDP).",
        "misconceptions": ["HTTP uses UDP by default", "HTTP includes its own transport"],
        "hints": ["Which protocol guarantees ordered delivery?"],
        "estimated_minutes": 4,
    },
    {
        "skill": "Git", "topic": "branching", "difficulty": "EASY",
        "question": "Which command creates and switches to a new branch?",
        "options": ["git merge", "git checkout -b <name>", "git commit -b", "git push --branch"],
        "correct_index": 1,
        "explanation": "git checkout -b (or git switch -c) creates a branch and moves HEAD to it.",
        "misconceptions": ["git branch switches automatically", "merge creates branches"],
        "hints": ["It combines branch creation with checkout."],
        "estimated_minutes": 4,
    },
    {
        "skill": "system design", "topic": "caching", "difficulty": "MEDIUM",
        "question": "The main risk of adding a cache in front of a database is:",
        "options": [
            "Caches cannot store strings",
            "Serving stale data when the underlying record changes",
            "Caches always increase latency",
            "Caches require schema changes",
        ],
        "correct_index": 1,
        "explanation": "Cache invalidation is the classic hard problem: stale entries must be expired or evicted on writes.",
        "misconceptions": ["Caching is always consistent", "Caches replace the database"],
        "hints": ["What happens right after a write?"],
        "estimated_minutes": 7,
    },
    {
        "skill": "ML", "topic": "model evaluation", "difficulty": "MEDIUM",
        "question": "A model with high training accuracy but poor test accuracy is most likely:",
        "options": ["Underfitting", "Overfitting", "Perfectly generalised", "Using too little data in training"],
        "correct_index": 1,
        "explanation": "Overfitting means the model memorised training noise and fails to generalise to unseen data.",
        "misconceptions": ["High training accuracy guarantees generalisation", "Underfitting shows high train accuracy"],
        "hints": ["Which failure mode memorises instead of learning?"],
        "estimated_minutes": 6,
    },
]

GENERIC_CONCEPTS: Dict[str, List[str]] = {
    "concept": [
        "Explain the core idea behind {topic} and give one concrete example.",
        "What are the trade-offs and failure modes of {topic}?",
        "Compare {topic} with the naive alternative: when does each win?",
    ],
    "practice": [
        "Implement a small, correct {topic} solution and walk through its complexity.",
        "Debug a broken {topic} implementation: find the invariant that is violated.",
        "Solve a realistic exercise on {topic} and write one edge-case test.",
    ],
}


def build(skill: str, count: int, difficulty: str, seed: str = "") -> List[Dict[str, Any]]:
    """Deterministic selection of real questions for a skill.

    Curated questions for the skill (matching difficulty first) are shuffled
    with a stable seed; any remainder is filled with generated conceptual
    tasks derived from the skill's learning objectives.
    """

    rng = random.Random(hashlib.sha256(f"{seed}:{skill}:{difficulty}".encode()).hexdigest())
    pool = [q for q in CURATED if q["skill"].lower() == skill.lower()]
    preferred = [q for q in pool if q["difficulty"] == difficulty]
    rest = [q for q in pool if q["difficulty"] != difficulty]
    rng.shuffle(preferred)
    rng.shuffle(rest)
    ordered = preferred + rest

    items: List[Dict[str, Any]] = []
    for q in ordered:
        if len(items) >= count:
            break
        items.append({**q, "objective": f"Demonstrate understanding of {q['topic']}",
                      "question_type": "mcq"})

    if len(items) < count:
        from .core.taxonomy import objectives_for

        topics = objectives_for(skill, 5)
        kinds = GENERIC_CONCEPTS["concept"] + GENERIC_CONCEPTS["practice"]
        index = 0
        while len(items) < count and topics:
            topic = topics[index % len(topics)]
            template = kinds[index % len(kinds)]
            items.append({
                "skill": skill,
                "topic": topic,
                "difficulty": difficulty,
                "question": template.format(topic=topic),
                "options": [],
                "correct_index": None,
                "answer": None,
                "explanation": "",
                "misconceptions": [],
                "hints": [f"Start from the simplest correct version of {topic}."],
                "estimated_minutes": 12,
                "objective": f"Apply {topic} confidently",
                "question_type": "open",
                "evaluation_criteria": ["Correctness", "Explanation", "Edge cases"],
            })
            index += 1
    return items

