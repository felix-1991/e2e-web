"""Track the tab and descendants that belong to one Playwright session."""

from urllib.parse import urlsplit, urlunsplit
from weakref import WeakSet

from .errors import E2EError


class Tabs:
    def __init__(self, page, *, own_initial):
        self.context = page.context
        self.initial = page
        self.own_initial = own_initial
        self.active = 1
        self.next_id = 2
        self.entries = {1: (page, None)}
        self.recent = [1]
        self.excluded = WeakSet(candidate for candidate in self.context.pages
                                if not own_initial and candidate is not page)

    def exclude(self, page):
        """Give a popup to its independent WebSession."""
        self.excluded.add(page)
        self.refresh()

    def refresh(self):
        known = {page: number for number, (page, _) in self.entries.items()}
        for page in self.context.pages:
            if page in known or page in self.excluded:
                continue
            opener = known.get(page.opener())
            if opener is not None:
                known[page] = self.next_id
                self.entries[self.next_id] = (page, opener)
                self.next_id += 1

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

    def summary(self):
        self.refresh()
        # Preserve the active tab and cap the metadata sent to the model.
        numbers = sorted({self.active, *list(self.entries)[:19]} & self.entries.keys())
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
