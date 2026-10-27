"""Per-project image generation settings."""
import json
from math import gcd
from PySide6.QtWidgets import (QDialog,QDialogButtonBox,QFormLayout,QComboBox,QLineEdit,
    QSpinBox,QDoubleSpinBox,QCheckBox,QPlainTextEdit,QPushButton,QFileDialog,QVBoxLayout,QLabel)
from prose_to_video.models.image_job import ImageGenerationSettings


class ImageGenerationSettingsDialog(QDialog):
    def __init__(self,settings,parent=None):
        super().__init__(parent);self.setWindowTitle('Image Generation Settings')
        self.provider=QComboBox();self.provider.addItems(['OpenAI','ComfyUI']);self.provider.setCurrentText(settings.provider)
        self.model=QLineEdit(settings.model)
        self.width=QSpinBox();self.width.setRange(64,3840);self.width.setValue(settings.width)
        self.height=QSpinBox();self.height.setRange(64,2160);self.height.setValue(settings.height)
        self.ratio=QLabel()
        self.quality=QComboBox();self.quality.addItems(['auto','low','medium','high','xhigh','max']);self.quality.setCurrentText(settings.quality)
        self.candidates=QSpinBox();self.candidates.setRange(1,10);self.candidates.setValue(settings.candidates)
        self.seed=QLineEdit('' if settings.seed is None else str(settings.seed))
        self.references=QCheckBox();self.references.setChecked(settings.use_references)
        self.format=QComboBox();self.format.addItems(['png','jpeg','webp']);self.format.setCurrentText(settings.output_format)
        self.concurrent=QSpinBox();self.concurrent.setRange(1,8);self.concurrent.setValue(settings.max_concurrent_jobs)
        self.batch_policy=QComboBox();self.batch_policy.addItem('Manual review and approval','manual')
        self.retries=QSpinBox();self.retries.setRange(0,5);self.retries.setValue(settings.retry_limit)
        self.cap=QLineEdit('' if settings.spending_cap is None else str(settings.spending_cap))
        self.estimate=QLineEdit('' if settings.estimated_cost_per_image is None else str(settings.estimated_cost_per_image))
        self.url=QLineEdit(settings.comfyui_url);self.workflow=QLineEdit(settings.workflow_path)
        browse=QPushButton('Import workflow JSON…');browse.clicked.connect(self._browse)
        self.mapping=QPlainTextEdit(json.dumps(settings.workflow_mapping,indent=2));self.mapping.setMinimumHeight(100)
        self.capabilities=QLabel();self.capabilities.setWordWrap(True)
        form=QFormLayout()
        for label,widget in [('Provider',self.provider),('Model',self.model),('Width',self.width),('Height',self.height),
            ('Aspect ratio',self.ratio),
            ('Quality',self.quality),('Candidates',self.candidates),('Seed (optional)',self.seed),
            ('Use approved references',self.references),('Output format',self.format),('Maximum concurrent jobs',self.concurrent),
            ('Batch approval policy',self.batch_policy),
            ('Automatic retry limit',self.retries),('Spending cap (optional)',self.cap),
            ('Estimated cost per image (optional)',self.estimate),('ComfyUI URL',self.url),
            ('Workflow path',self.workflow),('Workflow mapping JSON',self.mapping)]: form.addRow(label,widget)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept);buttons.rejected.connect(self.reject)
        layout=QVBoxLayout(self);layout.addLayout(form);layout.addWidget(browse)
        layout.addWidget(self.capabilities);layout.addWidget(buttons)
        self.provider.currentTextChanged.connect(self._provider_changed)
        self.model.textChanged.connect(self._refresh_capabilities)
        self.mapping.textChanged.connect(self._refresh_capabilities)
        self.width.valueChanged.connect(self._refresh_ratio)
        self.height.valueChanged.connect(self._refresh_ratio)
        self._refresh_ratio()
        self._refresh_capabilities()

    def _refresh_ratio(self,*_):
        divisor=gcd(self.width.value(),self.height.value())
        self.ratio.setText(f'{self.width.value()//divisor}:{self.height.value()//divisor}')

    def _provider_changed(self,name):
        if name=='ComfyUI':
            self.model.clear();self.quality.setCurrentText('auto');self.format.setCurrentText('png')
        elif not self.model.text().strip():self.model.setText('gpt-image-2.5-flare')
        self._refresh_capabilities()

    def _refresh_capabilities(self,*_):
        try:
            if self.provider.currentText()=='OpenAI':
                from prose_to_video.pipeline.image_providers.openai_provider import OpenAIImageProvider
                c=OpenAIImageProvider().capabilities(self.model.text().strip())
            else:
                from prose_to_video.pipeline.image_providers.comfyui_provider import ComfyUIProvider
                c=ComfyUIProvider(mapping=json.loads(self.mapping.toPlainText())).capabilities(self.model.text().strip())
            self.seed.setEnabled(c.seeds);self.references.setEnabled(c.references)
            self.candidates.setMaximum(c.max_candidates)
            for index in range(self.quality.count()):
                self.quality.model().item(index).setEnabled(self.quality.itemText(index) in c.qualities)
            if self.quality.currentText() not in c.qualities:self.quality.setCurrentText('auto')
            for index in range(self.format.count()):
                self.format.model().item(index).setEnabled(self.format.itemText(index) in c.formats)
            if self.format.currentText() not in c.formats:self.format.setCurrentText(c.formats[0])
            self.capabilities.setText(f'References: {c.max_references}; seeds: {"yes" if c.seeds else "no"}; '
                f'editing: {"yes" if c.editing else "no"}; maximum candidates: {c.max_candidates}')
        except Exception as exc:self.capabilities.setText(str(exc))

    def _browse(self):
        path,_=QFileDialog.getOpenFileName(self,'API workflow JSON','','JSON (*.json)')
        if path:self.workflow.setText(path)

    def values(self):
        return ImageGenerationSettings(provider=self.provider.currentText(),model=self.model.text().strip(),
            width=self.width.value(),height=self.height.value(),quality=self.quality.currentText(),
            candidates=self.candidates.value(),seed=int(self.seed.text()) if self.seed.text().strip() else None,
            use_references=self.references.isChecked(),output_format=self.format.currentText(),
            max_concurrent_jobs=self.concurrent.value(),retry_limit=self.retries.value(),
            batch_approval=self.batch_policy.currentData(),
            spending_cap=float(self.cap.text()) if self.cap.text().strip() else None,
            estimated_cost_per_image=float(self.estimate.text()) if self.estimate.text().strip() else None,
            comfyui_url=self.url.text().strip(),workflow_path=self.workflow.text().strip(),
            workflow_mapping=json.loads(self.mapping.toPlainText()))

    def _accept(self):
        from PySide6.QtWidgets import QMessageBox
        try:self.values()
        except (ValueError,TypeError) as exc:QMessageBox.warning(self,'Invalid settings',str(exc));return
        self.accept()
