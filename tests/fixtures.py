"""Сырые JSON-фикстуры в форме ответов Jira / Bitbucket."""


def jira_issue_raw(
    key="PROJ-1",
    issue_id="10001",
    status="In Progress",
    resolution=None,
    comments=None,
    summary="Тестовая задача",
    attachments=None,
):
    comments = comments if comments is not None else []
    attachments = attachments if attachments is not None else []
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status},
            "assignee": {"displayName": "Alice", "name": "alice"},
            "reporter": {"displayName": "Боб", "name": "bob"},
            "priority": {"name": "High"},
            "created": "2026-05-01T10:00:00.000+0300",
            "updated": "2026-05-20T10:00:00.000+0300",
            "resolution": {"name": resolution} if resolution else None,
            "description": "Описание задачи",
            "comment": {
                "comments": [
                    {
                        "id": str(c["id"]),
                        "author": {"displayName": c.get("author", "Кто-то"), "name": c.get("name", "user")},
                        "body": c.get("body", ""),
                        "created": c.get("created", "2026-05-10T12:00:00.000+0300"),
                        "updated": c.get("updated", "2026-05-10T12:00:00.000+0300"),
                    }
                    for c in comments
                ]
            },
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
                    "outwardIssue": {
                        "key": "PROJ-2",
                        "fields": {"summary": "Связанная", "status": {"name": "Open"}},
                    },
                }
            ],
            "attachment": [
                {
                    "id": str(a["id"]),
                    "filename": a.get("filename", "file.bin"),
                    "mimeType": a.get("mime", "application/octet-stream"),
                    "size": a.get("size", 0),
                    "created": a.get("created", "2026-05-10T12:00:00.000+0300"),
                    "author": {"displayName": a.get("author", "Кто-то")},
                    "content": a.get("content", "https://jira.test/secure/attachment/"
                                      f"{a['id']}/{a.get('filename', 'file.bin')}"),
                }
                for a in attachments
            ],
        },
    }


def jira_search_raw(issues, total=None, start_at=0, max_results=50):
    return {
        "startAt": start_at,
        "maxResults": max_results,
        "total": total if total is not None else len(issues),
        "issues": issues,
    }


def dev_status_pr_raw():
    return {
        "detail": [
            {
                "pullRequests": [
                    {"id": "#42", "name": "Fix bug", "url": "https://git/pr/42", "status": "OPEN"}
                ],
                "repositories": [],
            }
        ]
    }


def dev_status_branch_raw():
    """Формат dataType=branch: ветки на уровне detail[], repository вложен в ветку."""
    return {
        "detail": [
            {
                "branches": [
                    {
                        "name": "PROJ-1-fix",
                        "url": "https://git/br",
                        "repository": {"name": "repo", "url": "https://git/repo"},
                    }
                ],
                "repositories": [],
                "pullRequests": [],
            }
        ]
    }


def dev_status_repo_raw():
    """Формат dataType=repository: репозитории с коммитами (веток тут нет)."""
    return {
        "detail": [
            {
                "repositories": [
                    {
                        "name": "repo",
                        "commits": [{"displayId": "abc123", "message": "msg", "url": "https://git/c"}],
                    }
                ],
                "pullRequests": [],
            }
        ]
    }


def bitbucket_pr_raw(pr_id=42, title="Fix bug", state="OPEN", project="PROJ", repo="repo"):
    return {
        "id": pr_id,
        "title": title,
        "state": state,
        "author": {"user": {"displayName": "Alice", "name": "alice"}},
        "createdDate": 1700000000000,
        "updatedDate": 1700000100000,
        "fromRef": {
            "displayId": "feature/x",
            "repository": {"slug": repo, "project": {"key": project}},
        },
        "toRef": {
            "displayId": "release-10.7",
            "repository": {"slug": repo, "project": {"key": project}},
        },
        "reviewers": [
            {"user": {"name": "rev1", "displayName": "Ревьюер"}, "approved": True, "status": "APPROVED"}
        ],
    }


def bitbucket_dashboard_raw(prs):
    return {"size": len(prs), "isLastPage": True, "values": prs}


def bitbucket_merge_raw(conflicted=False, can_merge=True):
    return {"canMerge": can_merge, "conflicted": conflicted, "vetoes": []}


def bitbucket_activities_raw():
    """activities новыми сверху: один inline-коммент с ответом + апрув."""
    return {
        "isLastPage": True,
        "values": [
            {"action": "APPROVED", "user": {"displayName": "Ревьюер"}},
            {
                "action": "COMMENTED",
                "commentAnchor": {"path": "README.md", "line": 228, "lineType": "ADDED"},
                "diff": {
                    "hunks": [
                        {"segments": [
                            {"type": "CONTEXT", "lines": [{"line": "## заголовок"}]},
                            {"type": "ADDED", "lines": [{"line": "новая строка"}]},
                        ]}
                    ]
                },
                "comment": {
                    "id": 100,
                    "text": "вопрос по строке",
                    "author": {"displayName": "Боб", "name": "bob"},
                    "createdDate": 1700000000000,
                    "comments": [
                        {"id": 101, "text": "ответ", "author": {"displayName": "Alice", "name": "alice"}, "comments": []}
                    ],
                },
            },
        ],
    }


def bitbucket_commits_raw(ids=("abc123def", "fff999aaa")):
    return {
        "isLastPage": True,
        "values": [
            {"id": i + "0" * 30, "displayId": i, "message": f"msg {i}", "author": {"name": "alice"}}
            for i in ids
        ],
    }
