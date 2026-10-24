"""Skill taxonomy, role competency models and dependency graph.

Everything here is declarative data: roles map to weighted skill sets, skills
map to prerequisites, and skills map to learning objectives used by the
roadmap and practice generators.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

ROLE_SKILLS: Dict[str, List[str]] = {
    "Software Engineer": ["Python", "DSA", "OOP", "Git", "SQL", "DBMS", "OS",
                          "CN", "system design", "testing"],
    "AI Engineer": ["Python", "DSA", "ML", "deep learning", "NLP", "SQL",
                    "Git", "system design"],
    "ML Engineer": ["Python", "ML", "statistics", "SQL", "DSA", "Docker", "Git"],
    "Data Scientist": ["Python", "statistics", "ML", "SQL", "Pandas", "visualization"],
    "Data Analyst": ["SQL", "Python", "Pandas", "statistics", "visualization", "Excel"],
    "DevOps Engineer": ["Linux", "Docker", "Kubernetes", "CI/CD", "AWS", "Git",
                        "networking"],
    "Frontend Developer": ["HTML", "CSS", "JavaScript", "React", "Git", "testing",
                           "UI design"],
    "Backend Developer": ["Python", "SQL", "DBMS", "APIs", "Docker", "Git",
                          "testing", "system design"],
}

DEPENDENCIES: Dict[str, List[str]] = {
    "DSA": ["programming basics"],
    "Recursion": ["Arrays"],
    "Dynamic programming": ["Recursion"],
    "Graphs": ["Trees"],
    "ML": ["Python", "statistics"],
    "deep learning": ["ML"],
    "NLP": ["ML", "Python"],
    "Pandas": ["Python"],
    "system design": ["DBMS", "CN", "APIs"],
    "DBMS": ["SQL"],
    "Kubernetes": ["Docker"],
    "CI/CD": ["Git"],
    "React": ["JavaScript", "HTML", "CSS"],
    "testing": ["programming basics"],
}

INTERVIEW_CRITICAL: Dict[str, List[str]] = {
    "Software Engineer": ["DSA", "system design", "OOP"],
    "AI Engineer": ["ML", "deep learning", "DSA"],
    "ML Engineer": ["ML", "statistics", "Python"],
    "Data Scientist": ["statistics", "ML", "SQL"],
    "Data Analyst": ["SQL", "statistics", "visualization"],
    "DevOps Engineer": ["Docker", "Kubernetes", "CI/CD"],
    "Frontend Developer": ["JavaScript", "React", "CSS"],
    "Backend Developer": ["APIs", "DBMS", "system design"],
}

TOPIC_OBJECTIVES: Dict[str, List[str]] = {
    "DSA": ["Arrays", "Strings", "Linked lists", "Trees", "Graphs", "Recursion",
            "Dynamic programming"],
    "Python": ["syntax and data types", "functions and comprehensions",
               "modules and packages", "exceptions", "OOP in Python"],
    "SQL": ["SELECT and filtering", "joins", "aggregation", "subqueries",
            "indexing basics"],
    "DBMS": ["ER modelling", "normalisation", "transactions", "indexing",
             "query optimisation"],
    "ML": ["regression", "classification", "model evaluation", "feature engineering",
           "overfitting control"],
    "statistics": ["descriptive statistics", "probability", "distributions",
                   "hypothesis testing", "correlation"],
    "system design": ["requirements and estimation", "caching", "load balancing",
                      "databases and sharding", "messaging"],
    "React": ["components and props", "state and hooks", "effects and data fetching",
              "routing", "performance"],
    "JavaScript": ["syntax and types", "functions and closures", "async programming",
                   "DOM", "modules"],
    "Docker": ["images and containers", "Dockerfiles", "volumes and networks",
               "compose", "registry workflows"],
    "Git": ["commits and branches", "merging and rebasing", "remotes",
            "conflict resolution", "workflows"],
    "OOP": ["classes and objects", "inheritance", "polymorphism",
            "encapsulation", "design patterns"],
    "OS": ["processes and threads", "scheduling", "memory management",
           "deadlocks", "file systems"],
    "CN": ["OSI model", "TCP vs UDP", "HTTP", "DNS", "network security"],
}

DEFAULT_TARGET_PROFICIENCY = 80.0


def role_for(name: str) -> str:
    """Resolve a free-text role to the closest known competency model."""

    key = (name or "").strip().lower()
    if not key:
        return "Software Engineer"
    for role in ROLE_SKILLS:
        if role.lower() == key:
            return role
    for role in ROLE_SKILLS:
        if key in role.lower() or role.lower() in key:
            return role
    return "Software Engineer"


def all_skills() -> List[str]:
    names = set()
    for skills in ROLE_SKILLS.values():
        names.update(skills)
    names.update(DEPENDENCIES)
    for deps in DEPENDENCIES.values():
        names.update(deps)
    names.update(TOPIC_OBJECTIVES)
    return sorted(names)


def objectives_for(skill: str, limit: int = 5) -> List[str]:
    topics = TOPIC_OBJECTIVES.get(skill)
    if topics:
        return topics[:limit]
    return [f"{skill} fundamentals", f"{skill} core patterns",
            f"{skill} applied practice", f"{skill} debugging and pitfalls",
            f"{skill} review"][:limit]


def find_skills(text: str, vocabulary: Optional[List[str]] = None) -> List[str]:
    """Whole-word, case-insensitive skill detection against the vocabulary."""

    if not text:
        return []
    vocabulary = vocabulary or all_skills()
    found = []
    for skill in vocabulary:
        pattern = r"(?<![\w.#])" + re.escape(skill) + r"(?![\w.#])"
        if re.search(pattern, text, re.IGNORECASE):
            found.append(skill)
    return found
