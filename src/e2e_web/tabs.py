"""Track the tab and descendants that belong to one Playwright session."""

import time
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakSet

from .errors import E2EError
from ._utils import digest


class Tabs:
    def __init__(self, page, *, own_initial, on_page=None):
        self.context = page.context
        self.initial = page
        self.own_initial = own_initial
        self.active = 1
        self.next_id = 2
        self.entries = {1: (page, None)}
        self.recent = [1]
        self.on_page = on_page
        self.excluded = WeakSet(candidate for candidate in self.context.pages
                                if not own_initial and candidate is not page)
        if on_page is not None:
            on_page(page)

    def exclude(self, pages):
        """Give a popup to its independent WebSession."""
        self.excluded.update(pages)
        self.refresh()

    def refresh(self):
        known = {page: number for number, (page, _) in self.entries.items()}
        for page in self.context.pages:
            if page in known or page in self.excluded:
                continue
            parent = page.opener()
            if parent is not None and parent in self.excluded:
                self.excluded.add(page)
                continue
            opener = known.get(parent)
            if opener is None and not self.own_initial:
                # In a borrowed context an unrelated page is the caller's, but a context
                # this session owns has no other author: a popup with rel=noopener is the app's.
                continue
            known[page] = self.next_id
            self.entries[self.next_id] = (page, opener)
            self.next_id += 1
            if self.on_page is not None:
                self.on_page(page)

        removed = {number for number, (page, _) in self.entries.items()
                   if page.is_closed() or page in self.excluded}
        if not removed:
            return
        fallback = self.entries[self.active][1] if self.active in self.entries else None
        for number in removed:
            del self.entries[number]
        self.recent = [number for number in self.recent if number in self.entries]
        if self.active not in self.entries and self.entries:
            self.active = fallback if fallback in self.entries else self.recent[-1] if self.recent else next(iter(self.entries))
            self._remember(self.active)

    def _remember(self, number):
        if number in self.recent:
            self.recent.remove(number)
        self.recent.append(number)

    @property
    def page(self):
        entry = self.entries.get(self.active)
        if entry is None or entry[0].is_closed():
            self.refresh()
        return self.entries.get(self.active, (self.initial, None))[0]

    def fingerprint(self):
        """Bind each session-local tab number to its opener and full URL."""
        self.refresh()
        return digest([self.active, [(number, opener, page.url)
                                     for number, (page, opener) in self.entries.items()]])

    def wait_for_fingerprint(self, expected, timeout):
        end = time.monotonic() + min(0.75, timeout)
        while self.fingerprint() != expected:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.page.wait_for_timeout(min(25, remaining * 1000))
        return True

    def summary(self):
        self.refresh()
        # Preserve the active tab and cap the metadata sent to the model.
        numbers = list(self.entries)[-20:]
        if self.active in self.entries and self.active not in numbers:
            numbers[0] = self.active
        tabs = [dict(tab=number, url=self._display_url(self.entries[number][0].url)) for number in numbers]
        return {'active_tab': self.active, 'tabs': tabs, 'tab_count': len(self.entries)}

    @staticmethod
    def _display_url(url):
        parts = urlsplit(url)
        if parts.scheme in {'http', 'https'}:
            return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))[:512]
        return 'about:blank' if url == 'about:blank' else f'{parts.scheme}:' if parts.scheme else ''

    def _get(self, number):
        self.refresh()
        if number not in self.entries:
            raise E2EError('LOCATOR_NOT_FOUND', 'tab is no longer open in this session')
        return self.entries[number][0]

    def switch(self, number):
        self._get(number).bring_to_front()
        self.active = number
        self._remember(number)

    def close(self, number):
        page = self._get(number)
        if len(self.entries) <= 1:
            raise E2EError('POLICY_DENIED', 'the last open tab cannot be closed')
        if page is self.initial and not self.own_initial:
            raise E2EError('POLICY_DENIED', 'the caller owns the initial page')
        was_active = self.active == number
        page.close()
        self.refresh()
        if was_active and self.entries:
            self.page.bring_to_front()

    def close_owned_pages(self):
        self.refresh()
        for page, _ in reversed(tuple(self.entries.values())):
            if not page.is_closed():
                page.close()
