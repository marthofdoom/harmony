"""Seed-based recommendations and a natural-language playlist builder."""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.tasks import on_main, run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    _format_suggestion_subtitle,
    _humanize_sources,  # noqa: F401 - re-exported for the existing unit tests
    action_status_page,
    attach_context_menu,
    error_status_page,
    open_list_popover,
    open_preferences,
    set_stack_status,
    status_page,
)

log = logging.getLogger(__name__)


def _looks_resolved(pick, resolved: list[Track]) -> bool:  # noqa: ANN001 - TrackIdea, kept loosely typed
    """Best-effort check for whether ``pick`` matches something in ``resolved``.

    ``PlaylistPlanner.resolve`` returns real catalog tracks without keeping a
    positional link back to the original ideas (unresolved picks are simply
    dropped), so this falls back to a fuzzy title/artist substring check
    purely to drive the status icon — the actual "Create" action always uses
    the authoritative ``resolved`` list, never this heuristic.
    """
    pick_title, pick_artist = pick.title.casefold(), pick.artist.casefold()
    for track in resolved:
        title, artist = track.title.casefold(), track.artist_name.casefold()
        if (pick_title in title or title in pick_title) and (pick_artist in artist or artist in pick_artist):
            return True
    return False


class DiscoverPage(Gtk.Box):
    """Recommendations section + AI playlist builder section, stacked vertically."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self._suggestions: list[object] = []
        self._suggestions_loading = False
        self._playlist_choices: list[Playlist] = []
        self._idea = None
        self._resolved_tracks: list[Track] = []
        self._services_for_ai: list[Service] = []

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=576)
        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                    margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self._reco_section = self._build_recommendations_section()
        self._content_box.append(self._reco_section)
        self._ai_section = self._build_ai_section()
        self._content_box.append(self._ai_section)
        clamp.set_child(self._content_box)
        scroller.set_child(clamp)
        self.append(scroller)

        # Rebuilt rather than merely re-revealed: the section renders a banner,
        # a "missing layer" page, or the full builder depending on how the AI
        # integration is configured, so switching between those states means
        # constructing different widgets.
        self.state.connect("integrations-changed", lambda *_a: self._rebuild_ai_section())

        self.state.connect("playlists-changed", lambda *_a: self._refresh_playlist_choices())
        # Providers are built off the main loop, so this page is normally
        # constructed before any of them exist. Without this the service
        # pickers would stay stuck on "no services configured" forever.
        self.state.connect("providers-changed", lambda *_a: self._refresh_service_choices())
        self._refresh_playlist_choices()
        self._refresh_service_choices()

    def _refresh_service_choices(self) -> None:
        """Rebuild both service pickers from the providers that are live now."""
        services = [s for s in Service if s in self.state.providers]
        labels = [s.label for s in services]
        # Both the recommendations picker and the AI picker only exist when
        # their section built real content (recommender configured / planner
        # configured with a key); without that, the section rendered a
        # placeholder or banner instead and the attribute was never set. Must
        # be checked with getattr, not assumed to exist just because __init__
        # unconditionally calls this on every "providers-changed" emission.
        dropdowns = []
        target_dropdown = getattr(self, "target_service_dropdown", None)
        if target_dropdown is not None:
            dropdowns.append(target_dropdown)
        ai_dropdown = getattr(self, "ai_target_dropdown", None)
        if ai_dropdown is not None:
            dropdowns.append(ai_dropdown)

        # Preserve the user's pick by identity (the Service itself), not by
        # raw index -- an index-based carry-over is only correct when the
        # provider list purely grows at the end. If it shrinks or reorders
        # (a provider drops out, or Service enum order interacts with
        # `providers` differently across a reload) the same index can now
        # point at a completely different service and silently retarget the
        # dropdown out from under the user.
        previous_services = self._services_for_ai
        for dropdown in dropdowns:
            previous_index = dropdown.get_selected()
            previous_service = (
                previous_services[previous_index]
                if 0 <= previous_index < len(previous_services)
                else None
            )
            dropdown.set_model(Gtk.StringList.new(labels))
            if previous_service is not None and previous_service in services:
                dropdown.set_selected(services.index(previous_service))

        self._services_for_ai = services
        self._update_recommendations_controls_visibility()

    # -- section 1: recommendations ------------------------------------------

    @staticmethod
    def _compact(page: Adw.StatusPage) -> Adw.StatusPage:
        """Tag an embedded status page ``.compact`` (its inline, in-page look)."""
        page.add_css_class("compact")
        return page

    def _build_recommendations_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        if self.state.recommender is None:
            box.append(self._compact(action_status_page(
                icon_name="starred-symbolic",
                title="Recommendations Aren't Set Up",
                description="Connect a music service in Preferences to get recommendations "
                "based on your playlists.",
                action_label="Open Preferences",
                on_action=lambda: open_preferences(self, "accounts"),
            )))
            return box

        group = Adw.PreferencesGroup(
            title="Recommendations",
            description="Find similar tracks based on one of your playlists.",
        )
        self.seed_dropdown = Adw.ComboRow(title="Seed playlist")
        self.target_service_dropdown = Adw.ComboRow(
            title="Target service",
            subtitle="Where suggested tracks will be matched and playlists created",
            model=Gtk.StringList.new([s.label for s in Service if s in self.state.providers]),
        )
        group.add(self.seed_dropdown)
        group.add(self.target_service_dropdown)
        # Stale results reference a specific (seed, target service) pair --
        # once either changes the old list no longer describes "the current
        # seed", so clear it rather than leave misleading results on screen.
        self.seed_dropdown.connect("notify::selected", self._on_seed_selection_changed)
        self.target_service_dropdown.connect("notify::selected", self._on_seed_selection_changed)

        action_box = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.regenerate_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Regenerate with the same seed",
            sensitive=False, css_classes=["flat"],
        )
        self.regenerate_button.connect("clicked", self._on_get_suggestions_clicked)
        self.get_suggestions_button = Gtk.Button(label="Get Suggestions", css_classes=["suggested-action"])
        self.get_suggestions_button.connect("clicked", self._on_get_suggestions_clicked)
        action_box.append(self.regenerate_button)
        action_box.append(self.get_suggestions_button)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        controls.append(group)
        controls.append(action_box)

        # Swaps between the controls above and a status page when there's
        # nothing to recommend from yet (no service, or no playlists on it) --
        # replaces the old fake "No services configured" dropdown entry.
        self.controls_stack = Gtk.Stack()
        self.controls_stack.add_named(controls, "controls")
        box.append(self.controls_stack)

        self.suggestions_progress_label = Gtk.Label(css_classes=["dim-label"],
                                                    justify=Gtk.Justification.CENTER, wrap=True)
        self.suggestions_progress_bar = Gtk.ProgressBar(width_request=320, halign=Gtk.Align.CENTER)
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                                margin_top=36, margin_bottom=36)
        progress_box.append(Gtk.Label(label="Finding Suggestions…", css_classes=["title-4"]))
        progress_box.append(self.suggestions_progress_bar)
        progress_box.append(self.suggestions_progress_label)

        self.suggestions_group = Adw.PreferencesGroup(title="Suggestions")
        self.suggestions_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.suggestions_group.add(self.suggestions_list)
        self.create_from_suggestions_button = Gtk.Button(
            label="Create Playlist…", valign=Gtk.Align.CENTER,
            css_classes=["suggested-action"], sensitive=False,
        )
        self.create_from_suggestions_button.connect("clicked", self._on_create_from_suggestions)
        self.suggestions_group.set_header_suffix(self.create_from_suggestions_button)

        self.suggestions_stack = Gtk.Stack()
        self.suggestions_stack.add_named(progress_box, "loading")
        self.suggestions_stack.add_named(self.suggestions_group, "results")
        set_stack_status(
            self.suggestions_stack, "idle",
            self._compact(status_page(
                icon_name="edit-find-symbolic", title="No Suggestions Yet",
                description="Choose a seed playlist and a target service, then select Get Suggestions.")),
        )
        box.append(self.suggestions_stack)

        self._update_recommendations_controls_visibility()
        return box

    def _update_recommendations_controls_visibility(self) -> None:
        """Show the seed/service controls, or a status page explaining why not.

        Called after every playlist/provider refresh; a no-op before the
        section has been built (recommender missing) or before it's been
        built yet (called mid-``__init__``, guarded by ``hasattr``).
        """
        if not hasattr(self, "controls_stack"):
            return
        has_service = any(s in self.state.providers for s in Service)
        has_seed = bool(self._playlist_choices)
        if has_service and has_seed:
            self.controls_stack.set_visible_child_name("controls")
        elif not has_service:
            set_stack_status(
                self.controls_stack, "empty",
                self._compact(action_status_page(
                    icon_name="starred-symbolic", title="Nothing to Recommend From Yet",
                    description="Add a music service in Preferences to get started.",
                    action_label="Open Preferences",
                    on_action=lambda: open_preferences(self, "accounts"),
                )),
            )
        else:
            set_stack_status(
                self.controls_stack, "empty",
                self._compact(status_page(
                    icon_name="starred-symbolic", title="Nothing to Recommend From Yet",
                    description="None of your playlists are available yet. Add one on the "
                    "Playlists page.")),
            )
        self._update_get_suggestions_sensitivity()

    def _update_get_suggestions_sensitivity(self) -> None:
        if not hasattr(self, "get_suggestions_button"):
            return
        has_service = any(s in self.state.providers for s in Service)
        has_seed = bool(self._playlist_choices)
        self.get_suggestions_button.set_sensitive(has_service and has_seed and not self._suggestions_loading)

    def _refresh_playlist_choices(self) -> None:
        if self.state.recommender is None:
            return
        by_service = self.state.all_playlists()
        choices: list[Playlist] = []
        for service in Service:
            choices.extend(by_service.get(service, []))
        self._playlist_choices = choices
        labels = [f"{p.title} ({p.service.label})" for p in choices]
        self.seed_dropdown.set_model(Gtk.StringList.new(labels))
        self._update_recommendations_controls_visibility()

    def _on_seed_selection_changed(self, *_args: object) -> None:
        """Invalidate on-screen suggestions once the seed or target changes.

        A no-op while nothing is showing (nothing to invalidate) or while a
        fetch is already in flight (its own completion callback owns the
        list at that point).
        """
        if self._suggestions_loading or not self._suggestions:
            return
        self._suggestions = []
        self.create_from_suggestions_button.set_sensitive(False)
        self.regenerate_button.set_sensitive(False)
        while row := self.suggestions_list.get_row_at_index(0):
            self.suggestions_list.remove(row)
        set_stack_status(
            self.suggestions_stack, "idle",
            self._compact(status_page(
                icon_name="edit-find-symbolic", title="No Suggestions Yet",
                description="Choose a seed playlist and a target service, then select Get Suggestions.")),
        )

    def _on_get_suggestions_clicked(self, _button: Gtk.Button) -> None:
        index = self.seed_dropdown.get_selected()
        if not (0 <= index < len(self._playlist_choices)):
            self.state.toast("Pick a seed playlist first.")
            return
        seed_playlist = self._playlist_choices[index]
        services = [s for s in Service if s in self.state.providers]
        service_index = self.target_service_dropdown.get_selected()
        if not (0 <= service_index < len(services)):
            self.state.toast("No target service available.")
            return
        target_service = services[service_index]
        target_provider = self.state.providers[target_service]
        seed_provider = self.state.providers.get(seed_playlist.service)
        if seed_provider is None:
            return

        self._suggestions_loading = True
        self.get_suggestions_button.set_sensitive(False)
        self.regenerate_button.set_sensitive(False)
        self.create_from_suggestions_button.set_sensitive(False)
        self.suggestions_progress_bar.set_fraction(0.0)
        self.suggestions_progress_label.set_label("Starting…")
        self.suggestions_stack.set_visible_child_name("loading")

        def work() -> list:
            seeds = seed_provider.get_playlist_tracks(seed_playlist.id)

            # `similar_to_tracks` invokes this from the worker thread doing
            # the fetch, never the main loop -- marshal every UI touch
            # through `on_main` so it lands safely on the GLib main loop
            # instead of writing to a widget off-thread.
            def on_progress(fraction: float, message: str) -> None:
                on_main(self._update_suggestions_progress, fraction, message)

            return self.state.recommender.similar_to_tracks(
                seeds, target_provider, limit=30, progress=on_progress
            )

        def done(suggestions: list) -> None:
            self._on_suggestions_done(suggestions, target_service)

        run_async(work, done, self._on_suggestions_error)

    def _update_suggestions_progress(self, fraction: float, message: str) -> None:
        self.suggestions_progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
        if message:
            self.suggestions_progress_label.set_label(message)

    def _on_suggestions_done(self, suggestions: list, target_service: Service) -> None:
        self._suggestions = suggestions
        self._suggestions_loading = False
        self._update_get_suggestions_sensitivity()
        self.regenerate_button.set_sensitive(True)
        while row := self.suggestions_list.get_row_at_index(0):
            self.suggestions_list.remove(row)
        if not suggestions:
            self.create_from_suggestions_button.set_sensitive(False)
            set_stack_status(
                self.suggestions_stack, "empty",
                self._compact(status_page(
                    icon_name="edit-find-symbolic", title="No Suggestions Found",
                    description="Try a different seed playlist, or check that recommendation "
                    "sources are set up in Preferences.")),
            )
            return
        target_label = target_service.label
        for suggestion in suggestions:
            self.suggestions_list.append(self._build_suggestion_row(suggestion, target_label))
        self.suggestions_group.set_title(f"Suggestions ({len(suggestions)})")
        resolved_any = any(getattr(s, "resolved", None) is not None for s in suggestions)
        self.create_from_suggestions_button.set_sensitive(resolved_any)
        self.suggestions_stack.set_visible_child_name("results")

    def _on_suggestions_error(self, exc: BaseException) -> None:
        self._suggestions_loading = False
        self._update_get_suggestions_sensitivity()
        self.regenerate_button.set_sensitive(bool(self._suggestions))
        self.state.toast(f"Couldn't get suggestions: {exc}")
        set_stack_status(self.suggestions_stack, "error",
                         self._compact(error_status_page(exc, title="Couldn't Get Suggestions")))

    # -- suggestion rows: humanized subtitle + per-row actions ----------------

    def _build_suggestion_row(self, suggestion: object, target_label: str) -> Adw.ActionRow:
        resolved: Track | None = getattr(suggestion, "resolved", None)
        sources = getattr(suggestion, "sources", None) or []
        subtitle = _format_suggestion_subtitle(suggestion.artist, sources, target_label)
        row = Adw.ActionRow(title=suggestion.title, subtitle=subtitle)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)

        if resolved is not None:
            play_button = Gtk.Button(icon_name="media-playback-start-symbolic",
                                      tooltip_text="Play on Device…", valign=Gtk.Align.CENTER,
                                      css_classes=["flat"])
            play_button.connect("clicked", lambda _b, t=resolved: self._open_device_popover(play_button, t))
            add_button = Gtk.Button(icon_name="list-add-symbolic",
                                     tooltip_text="Add to Playlist…", valign=Gtk.Align.CENTER,
                                     css_classes=["flat"])
            add_button.connect("clicked", lambda _b, t=resolved: self._open_playlist_popover(add_button, t))
            row.add_suffix(play_button)
            row.add_suffix(add_button)

            def build_actions(t: Track = resolved) -> list[tuple[str, Callable[[], None]]]:
                return [
                    ("Add to Playlist…", lambda: self._open_playlist_popover(row, t)),
                    ("Play on Device…", lambda: self._open_device_popover(row, t)),
                ]

            attach_context_menu(row, build_actions)
        else:
            icon = Gtk.Image.new_from_icon_name("dialog-question-symbolic")
            icon.add_css_class("dim-label")
            icon.set_tooltip_text("No playable match found — it won't be added to playlists")
            row.add_prefix(icon)
            row.add_css_class("dim-label")
        return row

    # -- per-suggestion actions: playlist/device popovers ------------------------
    #
    # Small, self-contained duplicates of similar_dialog's
    # `_open_playlist_popover`/`_open_device_popover` idiom (single-track
    # variants) rather than a shared import, matching the precedent it set
    # for itself -- this page only ever acts on one resolved suggestion at a
    # time too.

    def _open_playlist_popover(self, parent: Gtk.Widget, track: Track) -> None:
        playlists_by_service = self.state.all_playlists()
        popover = Gtk.Popover()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        found = False
        for service in Service:
            provider = self.state.providers.get(service)
            if provider is None:
                continue
            for playlist in playlists_by_service.get(service, []):
                row = Adw.ActionRow(title=playlist.title,
                                     subtitle=f"{service.label} · {playlist.track_count or 0} tracks")
                row.set_activatable(True)

                def _pick(_row: Adw.ActionRow, p: Playlist = playlist, pop: Gtk.Popover = popover) -> None:
                    pop.popdown()
                    self._add_track_to_playlist(track, p)

                row.connect("activated", _pick)
                listbox.append(row)
                found = True
        if not found:
            listbox.append(Adw.ActionRow(title="No playlists yet", sensitive=False))
        open_list_popover(popover, parent, listbox)

    def _add_track_to_playlist(self, track: Track, playlist: Playlist) -> None:
        provider = self.state.providers.get(playlist.service)
        if provider is None:
            self.state.toast(f"No provider configured for {playlist.service.label}")
            return
        track_id = track.id

        def work() -> None:
            provider.add_tracks(playlist.id, [track_id])

        def done(_result: None) -> None:
            self.state.toast(f"Added “{track.title}” to {playlist.title}")
            self.state.all_playlists(refresh=True)
            self.state.emit("playlist-tracks-changed", playlist)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't add track: {exc}"))

    def _open_device_popover(self, parent: Gtk.Widget, track: Track) -> None:
        devices = self.state.playback_targets()
        popover = Gtk.Popover()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        if not devices:
            listbox.append(Adw.ActionRow(title="No devices yet", subtitle="Add one on the Devices page",
                                          sensitive=False))
        for info in devices:
            is_local = info.kind == "local"
            row = Adw.ActionRow(title=info.name,
                                subtitle="In this app" if is_local else info.host)
            row.set_activatable(True)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "computer-symbolic" if is_local else "audio-speakers-symbolic"))

            def _pick(_row: Adw.ActionRow, host: str = info.host, name: str = info.name,
                      pop: Gtk.Popover = popover) -> None:
                pop.popdown()
                self._play_track_on_device(track, host, name)

            row.connect("activated", _pick)
            listbox.append(row)
        open_list_popover(popover, parent, listbox)

    def _play_track_on_device(self, track: Track, host: str, name: str) -> None:
        def work() -> None:
            self.state.play_track_on_device(track, host)

        def done(_result: None) -> None:
            self.state.toast(f"Playing “{track.title}” on {name}")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't play on {name}: {exc}"))

    # -- footer: create playlist from resolved suggestions -----------------------

    def _on_create_from_suggestions(self, _button: Gtk.Button) -> None:
        resolved = [s.resolved for s in self._suggestions if getattr(s, "resolved", None) is not None]
        if not resolved:
            self.state.toast("None of these suggestions resolved to a real track.")
            return
        services = [s for s in Service if s in self.state.providers]
        service_index = self.target_service_dropdown.get_selected()
        target_service = services[service_index] if 0 <= service_index < len(services) else resolved[0].service
        self._prompt_playlist_name(target_service, resolved)

    def _prompt_playlist_name(self, service: Service, tracks: list[Track]) -> None:
        dialog = Adw.AlertDialog(heading="New Playlist")
        entry = Adw.EntryRow(title="Title", text="Discover Mix")
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "create":
                return
            title = entry.get_text().strip() or "Discover Mix"
            self._create_and_fill(service, title, tracks)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _create_and_fill(self, service: Service, title: str, tracks: list[Track]) -> None:
        provider = self.state.providers[service]
        ids = [t.id for t in tracks]

        def work() -> Playlist:
            playlist = provider.create_playlist(title)
            provider.add_tracks(playlist.id, ids)
            return playlist

        def done(_playlist: Playlist) -> None:
            count = len(ids)
            self.state.toast(
                GLib.dngettext(None, "Created “%s” with %d track", "Created “%s” with %d tracks", count)
                % (title, count)
            )
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't create playlist: {exc}"))

    # -- section 2: AI playlist builder ---------------------------------------

    def _open_integrations_preferences(self) -> None:
        root = self.get_root()
        app = root.get_application() if root is not None else None
        if app is not None and hasattr(app, "open_preferences"):
            app.open_preferences("integrations")
        else:  # pragma: no cover - fallback if the widget is unrooted
            self.activate_action("app.preferences", None)

    def _rebuild_ai_section(self) -> None:
        """Swap in a freshly-built AI section after the integration changed."""
        if not hasattr(self, "_content_box"):
            return
        # Anything the old section owned is about to be destroyed; drop the
        # references so a stale widget can't be written into later.
        for attr in ("prompt_view", "count_spin", "ai_target_dropdown"):
            if hasattr(self, attr):
                delattr(self, attr)
        self._idea = None
        self._resolved_tracks = []

        new_section = self._build_ai_section()
        self._content_box.remove(self._ai_section)
        self._content_box.append(new_section)
        self._ai_section = new_section
        self._refresh_service_choices()

    def _build_ai_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        planner = self.state.planner
        if planner is None:
            group = Adw.PreferencesGroup(title="AI Playlist Builder")
            group.add(Adw.ActionRow(
                title="Not available",
                subtitle="This feature isn't included in this build of Harmony.",
                sensitive=False,
            ))
            box.append(group)
            return box
        if not planner.available:
            group = Adw.PreferencesGroup(title="AI Playlist Builder")
            row = Adw.ActionRow(
                title="Set up the AI playlist builder",
                subtitle="Add an Anthropic API key in Preferences to describe a playlist in plain "
                "words and have it built for you.",
            )
            button = Gtk.Button(label="Open Preferences", valign=Gtk.Align.CENTER,
                                css_classes=["suggested-action"])
            button.connect("clicked", lambda *_a: self._open_integrations_preferences())
            row.add_suffix(button)
            group.add(row)
            box.append(group)
            return box

        group = Adw.PreferencesGroup(
            title="AI Playlist Builder",
            description="Describe the playlist you want — mood, era, artists — and Harmony will "
            "plan it and match real tracks.",
        )

        # The prompt must read first; ``PreferencesGroup.add`` would push a
        # non-row child *below* the listbox, so the prompt lives outside the
        # group, above it, and only the two setting rows go inside.
        self.prompt_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, height_request=90,
                                         top_margin=10, bottom_margin=10, left_margin=12, right_margin=12)
        prompt_frame = Gtk.Frame(css_classes=["card"], margin_bottom=6)
        prompt_frame.set_child(self.prompt_view)

        self.count_spin = Adw.SpinRow.new_with_range(5, 100, 1)
        self.count_spin.set_title("Number of tracks")
        self.count_spin.set_value(25)
        services = [s for s in Service if s in self.state.providers]
        self.ai_target_dropdown = Adw.ComboRow(
            title="Target service",
            model=Gtk.StringList.new([s.label for s in services]),
        )
        self.ai_target_dropdown.set_sensitive(bool(services))
        group.add(self.count_spin)
        group.add(self.ai_target_dropdown)

        action_box = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        generate_button = Gtk.Button(label="Generate", css_classes=["suggested-action"])
        generate_button.connect("clicked", self._on_generate_clicked)
        action_box.append(generate_button)

        self._services_for_ai = services
        self.idea_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        box.append(prompt_frame)
        box.append(group)
        box.append(action_box)
        box.append(self.idea_box)
        return box

    def _on_generate_clicked(self, _button: Gtk.Button) -> None:
        buffer = self.prompt_view.get_buffer()
        prompt = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if not prompt:
            self.state.toast("Describe the playlist you want first.")
            return
        if not self._services_for_ai:
            self.state.toast("No services configured to resolve tracks against.")
            return
        count = int(self.count_spin.get_value())
        planner = self.state.planner
        target_service = self._services_for_ai[self.ai_target_dropdown.get_selected()]
        provider = self.state.providers[target_service]

        while child := self.idea_box.get_first_child():
            self.idea_box.remove(child)
        spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spinner.set_margin_top(24)
        spinner.set_halign(Gtk.Align.CENTER)
        self.idea_box.append(spinner)

        def work():  # noqa: ANN202 - PlaylistIdea
            idea = planner.plan(prompt, count=count)
            resolved = planner.resolve(idea, provider)
            return idea, resolved

        run_async(work, self._on_idea_done, self._on_idea_error)

    def _on_idea_error(self, exc: BaseException) -> None:
        while child := self.idea_box.get_first_child():
            self.idea_box.remove(child)
        self.state.toast(f"Couldn't generate playlist: {exc}")

    def _on_idea_done(self, result: tuple) -> None:
        idea, resolved = result
        self._idea = idea
        self._resolved_tracks = resolved
        while child := self.idea_box.get_first_child():
            self.idea_box.remove(child)

        group = Adw.PreferencesGroup(
            title=idea.title or "Untitled Playlist",
            description=getattr(idea, "description", "") or "",
        )
        create_button = Gtk.Button(
            label=f"Create Playlist ({len(resolved)} matched)", valign=Gtk.Align.CENTER,
            css_classes=["suggested-action"], sensitive=bool(resolved),
        )
        create_button.connect("clicked", self._on_create_ai_playlist)
        group.set_header_suffix(create_button)

        for pick in getattr(idea, "tracks", []):
            is_resolved = _looks_resolved(pick, resolved)
            row = Adw.ActionRow(title=pick.title, subtitle=f"{pick.artist} · {pick.why}")
            if not is_resolved:
                row.add_css_class("dim-label")
                icon = Gtk.Image.new_from_icon_name("dialog-question-symbolic")
                icon.add_css_class("dim-label")
                icon.set_tooltip_text("No playable match found — it won't be added to playlists")
                row.add_suffix(icon)
            group.add(row)
        self.idea_box.append(group)

    def _on_create_ai_playlist(self, _button: Gtk.Button) -> None:
        if not self._resolved_tracks or self._idea is None:
            return
        target_service = self._services_for_ai[self.ai_target_dropdown.get_selected()]
        self._create_and_fill(target_service, self._idea.title or "AI Playlist", self._resolved_tracks)
