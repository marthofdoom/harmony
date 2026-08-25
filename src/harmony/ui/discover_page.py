"""Seed-based recommendations and a natural-language playlist builder."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import missing_layer_status_page  # noqa: E402

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
        self._playlist_choices: list[Playlist] = []
        self._idea = None
        self._resolved_tracks: list[Track] = []

        scroller = Gtk.ScrolledWindow(vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                           margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        content.append(self._build_recommendations_section())
        content.append(Gtk.Separator())
        content.append(self._build_ai_section())
        scroller.set_child(content)
        self.append(scroller)

        self.state.connect("playlists-changed", lambda *_a: self._refresh_playlist_choices())
        self._refresh_playlist_choices()

    # -- section 1: recommendations ------------------------------------------

    def _build_recommendations_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(Gtk.Label(label="Recommendations", xalign=0.0, css_classes=["title-2"]))

        if self.state.recommender is None:
            box.append(missing_layer_status_page("recommender"))
            return box

        controls = Gtk.Box(spacing=8)
        self.seed_dropdown = Gtk.DropDown(hexpand=True)
        self.target_service_dropdown = Gtk.DropDown.new_from_strings(
            [s.label for s in Service if s in self.state.providers] or ["No services configured"]
        )
        get_button = Gtk.Button(label="Get Suggestions", css_classes=["suggested-action"])
        get_button.connect("clicked", self._on_get_suggestions_clicked)
        controls.append(Gtk.Label(label="Seed playlist:"))
        controls.append(self.seed_dropdown)
        controls.append(self.target_service_dropdown)
        controls.append(get_button)
        box.append(controls)

        self.suggestions_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.suggestions_list.add_css_class("boxed-list")
        box.append(self.suggestions_list)

        self.create_from_suggestions_button = Gtk.Button(label="Create Playlist from These", sensitive=False)
        self.create_from_suggestions_button.connect("clicked", self._on_create_from_suggestions)
        box.append(self.create_from_suggestions_button)
        return box

    def _refresh_playlist_choices(self) -> None:
        if self.state.recommender is None:
            return
        by_service = self.state.all_playlists()
        choices: list[Playlist] = []
        for service in Service:
            choices.extend(by_service.get(service, []))
        self._playlist_choices = choices
        labels = [f"{p.title} ({p.service.label})" for p in choices] or ["No playlists available"]
        self.seed_dropdown.set_model(Gtk.StringList.new(labels))

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

        def work() -> list:
            seeds = seed_provider.get_playlist_tracks(seed_playlist.id)
            return self.state.recommender.similar_to_tracks(seeds, target_provider, limit=30)

        run_async(work, self._on_suggestions_done, lambda exc: self.state.toast(f"Couldn't get suggestions: {exc}"))

    def _on_suggestions_done(self, suggestions: list) -> None:
        self._suggestions = suggestions
        while row := self.suggestions_list.get_row_at_index(0):
            self.suggestions_list.remove(row)
        if not suggestions:
            self.suggestions_list.append(Adw.ActionRow(title="No suggestions found"))
            self.create_from_suggestions_button.set_sensitive(False)
            return
        for suggestion in suggestions:
            badges = " · ".join(getattr(suggestion, "sources", []))
            resolved = getattr(suggestion, "resolved", None)
            subtitle = f"{suggestion.artist} · {badges} · score {suggestion.score:.2f}"
            row = Adw.ActionRow(title=suggestion.title, subtitle=subtitle)
            icon = "emblem-ok-symbolic" if resolved is not None else "dialog-question-symbolic"
            row.add_suffix(Gtk.Image.new_from_icon_name(icon))
            self.suggestions_list.append(row)
        self.create_from_suggestions_button.set_sensitive(True)

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
            self.state.toast(f"Created “{title}” with {len(ids)} track(s)")
            self.state.all_playlists(refresh=True)

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't create playlist: {exc}"))

    # -- section 2: AI playlist builder ---------------------------------------

    def _build_ai_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(Gtk.Label(label="AI Playlist Builder", xalign=0.0, css_classes=["title-2"]))

        planner = self.state.planner
        if planner is None:
            box.append(missing_layer_status_page("ai"))
            return box
        if not planner.available:
            banner = Adw.Banner(
                title="Add an Anthropic API key in Preferences → Integrations to enable this.",
                revealed=True,
            )
            banner.set_button_label("Open Preferences")
            banner.connect("button-clicked", lambda *_a: self.activate_action("app.preferences", None))
            box.append(banner)
            return box

        frame = Gtk.Frame()
        self.prompt_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=8, bottom_margin=8,
                                         left_margin=8, right_margin=8, height_request=90)
        frame.set_child(self.prompt_view)
        box.append(frame)

        controls = Gtk.Box(spacing=8)
        controls.append(Gtk.Label(label="Tracks:"))
        self.count_spin = Gtk.SpinButton.new_with_range(5, 100, 1)
        self.count_spin.set_value(25)
        controls.append(self.count_spin)
        services = [s for s in Service if s in self.state.providers]
        self.ai_target_dropdown = Gtk.DropDown.new_from_strings(
            [s.label for s in services] or ["No services configured"]
        )
        controls.append(self.ai_target_dropdown)
        generate_button = Gtk.Button(label="Generate", css_classes=["suggested-action"])
        generate_button.connect("clicked", self._on_generate_clicked)
        controls.append(generate_button)
        box.append(controls)

        self._services_for_ai = services
        self.idea_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
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
        self.idea_box.append(Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True))

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

        self.idea_box.append(Gtk.Label(label=idea.title or "Untitled playlist", xalign=0.0, css_classes=["heading"]))
        if getattr(idea, "description", ""):
            self.idea_box.append(Gtk.Label(label=idea.description, xalign=0.0, wrap=True))

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for pick in getattr(idea, "tracks", []):
            is_resolved = _looks_resolved(pick, resolved)
            row = Adw.ActionRow(title=pick.title, subtitle=f"{pick.artist} — {pick.why}")
            icon = "emblem-ok-symbolic" if is_resolved else "dialog-warning-symbolic"
            row.add_suffix(Gtk.Image.new_from_icon_name(icon))
            listbox.append(row)
        self.idea_box.append(listbox)

        create_button = Gtk.Button(label=f"Create Playlist ({len(resolved)} resolved)")
        create_button.set_sensitive(bool(resolved))
        create_button.connect("clicked", self._on_create_ai_playlist)
        self.idea_box.append(create_button)

    def _on_create_ai_playlist(self, _button: Gtk.Button) -> None:
        if not self._resolved_tracks or self._idea is None:
            return
        target_service = self._services_for_ai[self.ai_target_dropdown.get_selected()]
        self._create_and_fill(target_service, self._idea.title or "AI Playlist", self._resolved_tracks)
