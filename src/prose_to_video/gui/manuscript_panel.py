"""Compact source-anchored manuscript structure review."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from prose_to_video.pipeline.manuscript import CLASSIFICATIONS, analysis_report, export_report
from prose_to_video.storage.manuscript import ManuscriptStore


class ManuscriptPanel(QWidget):
    navigate = Signal(int, int)
    decision_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = None
        self.text = ""
        self.sections = []
        self.alignment = None
        self.summary = QLabel("Import a manuscript to review its structure.")
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Section", "Source range", "Classification", "Include", "Narration", "Format / viewpoint", "Review"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self._selected)
        self.warnings = QListWidget()
        self.warnings.itemClicked.connect(lambda item: self.navigate.emit(*item.data(256)))
        self.classification = QComboBox()
        self.classification.addItems(CLASSIFICATIONS)
        self.include = QCheckBox("Include body in narration")
        self.review = QCheckBox("Requires review")
        self.save = QPushButton("Save decision")
        self.save.clicked.connect(self._save)
        self.export = QPushButton("Export report…")
        self.export.clicked.connect(self._export)
        controls = QHBoxLayout()
        for widget in (self.classification, self.include, self.review, self.save, self.export):
            controls.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Chronology warnings (select to view source)"))
        layout.addWidget(self.warnings)
        layout.addLayout(controls)
        self.save.setEnabled(False)
        self.export.setEnabled(False)

    def bind(self, database, text: str = ""):
        self.store = ManuscriptStore(database) if database else None
        self.text = text
        self.alignment = None
        self.reload()

    def set_alignment(self, run):
        self.alignment = run
        self.reload()

    def reload(self):
        self.sections = self.store.sections(self.text) if self.store and self.text else []
        report = analysis_report(self.text, self.sections)
        self.summary.setText(f"{report['characters']:,} characters · {report['section_count']} sections · "
            f"{report['review_count']} to review · {report['excluded_count']} excluded · "
            f"{len(report['chronology_conflicts'])} chronology warnings")
        self.warnings.clear()
        for issue in report["chronology_conflicts"]:
            first, second = issue["claims"]
            item = QListWidgetItem(f"{issue['character']}: age {first['age']} in {first['year']} vs "
                f"age {second['age']} in {second['year']}")
            item.setData(256, (first["source_start"], first["source_end"]))
            self.warnings.addItem(item)
        self.table.setRowCount(len(self.sections))
        for row, section in enumerate(self.sections):
            aligned = ([w for w in self.alignment.words if section.body_start <= w.source_start < section.end]
                       if self.alignment else [])
            match_status = ("Excluded" if not section.included else
                "Not aligned" if self.alignment is None else
                "Matched" if aligned and all(w.audio_start is not None for w in aligned if w.status != "excluded") else
                "Partial / unresolved")
            cells = (section.heading or "(Preamble)", f"{section.start}:{section.end}",
                section.classification, "Yes" if section.included else "No", match_status,
                ", ".join(section.formats) + f" / {section.viewpoint}", "Yes" if section.review else "No")
            for column, value in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.export.setEnabled(bool(self.sections))
        self.save.setEnabled(False)

    def _selected(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self.sections):
            self.save.setEnabled(False)
            return
        section = self.sections[row]
        self.classification.setCurrentText(section.classification)
        self.include.setChecked(section.included)
        self.review.setChecked(section.review)
        self.save.setEnabled(True)
        self.navigate.emit(section.start, section.end)

    def _save(self):
        row = self.table.currentRow()
        if self.store is None or not 0 <= row < len(self.sections): return
        section = self.sections[row]
        inclusion_changed = section.included != self.include.isChecked()
        self.store.decide(self.text, section.identifier, self.classification.currentText(),
                          self.include.isChecked(), self.review.isChecked())
        self.reload()
        self.table.selectRow(row)
        self.decision_changed.emit(inclusion_changed)

    def _export(self):
        if not self.sections: return
        path, _ = QFileDialog.getSaveFileName(self, "Export manuscript analysis", "manuscript-analysis.json",
                                               "JSON (*.json);;Markdown (*.md)")
        if path:
            export_report(__import__("pathlib").Path(path), analysis_report(self.text, self.sections))
