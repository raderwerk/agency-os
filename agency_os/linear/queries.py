"""Elk GraphQL-document van de Spil, als moduleconstante.

Veldlijsten staan hier en nergens anders, zodat één plek te controleren is
tegen `hq/linear/graphql-cheatsheet.md`.

Twee documenten zijn veiligheidskritisch:

* `ISSUE_UPDATE` kent alleen `addedLabelIds` en `removedLabelIds`. Het veld
  `labelIds` vervangt de hele labelset en komt daarom in geen enkel document
  voor (spec 1.5, docs/architecture.md 13). Een test controleert dat letterlijk.
* `ISSUE_HISTORY` is onbevestigd (architectuur 18.6): als het veld niet
  bruikbaar blijkt, degradeert `gates.py` naar afleiding uit het
  handelingenlogboek in plaats van te raden.
"""

from __future__ import annotations

__all__ = [
    "ISSUE_FIELDS",
    "ISSUE_BY_ID",
    "POLL",
    "ISSUE_COMMENTS",
    "AGENT_SESSIONS",
    "LABELS",
    "WORKFLOW_STATES",
    "ORGANIZATION",
    "ISSUE_HISTORY",
    "ISSUE_UPDATE",
    "COMMENT_CREATE",
    "ATTACHMENT_LINK_URL",
    "VIEWER",
]

# Eén veldlijst voor elk issue dat we lezen. `labels` geeft leaf + parent terug;
# de canonieke naam wordt in client.py samengesteld.
ISSUE_FIELDS = """
  id
  identifier
  title
  description
  url
  priority
  estimate
  updatedAt
  team { key }
  state { id name type }
  project { id name }
  assignee { id }
  delegate { id }
  labels(first: 30) { nodes { id name parent { name } } }
"""

ISSUE_BY_ID = """
query IssueById($id: String!) {
  issue(id: $id) { %s }
}
""" % ISSUE_FIELDS

# De ene gebatchte leesronde per cyclus. Statussen worden clientzijdig
# gefilterd: `state.type nin [...]` is niet geverifieerd tegen deze workspace en
# een filter dat stil niets teruggeeft is erger dan een iets duurdere query.
POLL = """
query Poll($teamKeys: [String!], $first: Int!, $after: String) {
  organization { id name createdIssueCount }
  issues(filter: { team: { key: { in: $teamKeys } } }, first: $first, after: $after) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}
""" % ISSUE_FIELDS

ISSUE_COMMENTS = """
query IssueComments($id: String!, $first: Int!) {
  issue(id: $id) {
    id
    comments(first: $first) {
      nodes { id body createdAt user { id name app } }
    }
  }
}
"""

AGENT_SESSIONS = """
query AgentSessions($id: String!) {
  issue(id: $id) {
    id
    agentSessions {
      nodes {
        id
        status
        summary
        createdAt
        updatedAt
        appUser { id name app }
        activities(first: 20) { nodes { id createdAt content } }
        pullRequests { nodes { url } }
      }
    }
  }
}
"""

LABELS = """
query Labels($first: Int!, $after: String) {
  issueLabels(first: $first, after: $after) {
    nodes { id name parent { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

WORKFLOW_STATES = """
query WorkflowStates($teamKey: String!, $first: Int!, $after: String) {
  workflowStates(filter: { team: { key: { eq: $teamKey } } }, first: $first, after: $after) {
    nodes { id name type }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ORGANIZATION = """
query Organization {
  organization { id name createdIssueCount }
}
"""

VIEWER = """
query Viewer {
  viewer { id name email }
}
"""

# Onbevestigd veld; zie de moduledocstring en architectuur 18.6.
ISSUE_HISTORY = """
query IssueHistory($id: String!, $first: Int!) {
  issue(id: $id) {
    id
    history(first: $first) {
      nodes {
        id
        createdAt
        actor { id name app }
        addedLabels { id name parent { name } }
        removedLabels { id name parent { name } }
        fromState { name }
        toState { name }
      }
    }
  }
}
"""

# GEEN `labelIds` -- alleen addedLabelIds / removedLabelIds.
ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier state { id name } }
  }
}
"""

COMMENT_CREATE = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id createdAt }
  }
}
"""

ATTACHMENT_LINK_URL = """
mutation AttachmentLinkUrl($issueId: String!, $url: String!, $title: String!) {
  attachmentLinkURL(issueId: $issueId, url: $url, title: $title) {
    success
    attachment { id }
  }
}
"""
