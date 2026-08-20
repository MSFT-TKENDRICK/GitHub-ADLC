# Add dark mode to the settings page

## Problem

Users working in low-light environments report eye strain because the settings
page only supports a light theme. Support has logged 42 requests in the last
quarter, and it is the top-voted item in the feedback board.

## Desired outcome

A user can switch the settings page between light and dark themes, so that the
interface is comfortable in any lighting condition and matches the operating
system preference by default.

## Acceptance criteria

- **US1-AC1**: A theme toggle is reachable from the settings page header.
- **US1-AC2**: Selecting a theme applies it immediately without a page reload.
- **US1-AC3**: The chosen theme persists across sessions and is covered by an
  automated test.

## Constraints and scope

- The change must not increase Largest Contentful Paint beyond 2500 ms.
- Colour contrast must meet WCAG AA (4.5:1 for body text).
- Out of scope: theming any page other than settings; a custom theme editor.

## Audience

Primarily existing end users and administrators who spend long sessions in the
settings area. Developers are a secondary audience via the shared theme tokens.
