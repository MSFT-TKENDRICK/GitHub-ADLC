# Feature Specification: Dark Mode for the Reader

## Problem

Readers currently cannot use the reader at night: the only theme is a bright
white page, and there is no way to switch. Support tickets about eye strain are
frequent, and the manual workaround (an OS-level colour inversion) breaks
images and is error-prone.

## User Scenarios & Testing

### User Story 1 — Reader switches theme (Priority: P1)

As a reader, I want to toggle a dark theme so that I can read at night without eye strain.

**Acceptance Scenarios**

1. **Given** the reader is on `/library`, **When** they open appearance settings
   and choose "Dark", **Then** the page repaints in the dark palette. (US1-AC1)
2. **Given** a dark theme is active, **When** they reload the page, **Then** the
   choice persists across sessions. (US1-AC2)

Contrast must meet WCAG AA and the toggle must be reachable by keyboard.

### User Story 2 — Administrator sets the workspace default (Priority: P2)

As an administrator, I want to set the workspace default theme so that new readers
inherit the house style. (US2-AC1)

The admin console is slow today and changing defaults by hand is error-prone.

## Requirements

### Functional Requirements

- **FR-001**: System MUST persist the selected theme per account.
- **FR-002**: System MUST respect `prefers-color-scheme` on first visit.
- **NFR-001**: Theme switch MUST repaint within 100 ms.

### Key Entities

- **Reader**: a person with an account who reads documents.
- **ThemePreference**: the stored theme choice for one reader.
- **Workspace**: a tenant that owns a default theme.

A Reader has many ThemePreference records. A ThemePreference belongs to a Workspace.

## Routes

The feature touches the `/library` page, the `/settings/appearance` screen and
the `/api/v1/preferences` endpoint.
