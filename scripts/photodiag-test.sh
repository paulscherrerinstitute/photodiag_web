source /opt/miniconda3/etc/profile.d/conda.sh
conda activate test

bokeh serve /opt/photodiag_web/photodiag_web/app --ico-path=none --port=5011
