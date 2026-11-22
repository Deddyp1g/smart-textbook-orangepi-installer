if [[ ${SMART_TEXTBOOK_PROFILE:-} == npu ]]; then
    source /usr/local/Ascend/ascend-toolkit/set_env.sh
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH%:}:/usr/local/Ascend/driver/lib64:/usr/lib64"
    export PYTHONPATH=/opt/textbook/runtime/python/site-packages${PYTHONPATH:+:$PYTHONPATH}
    export SMART_TEXTBOOK_RUNTIME_ROOT=/opt/textbook
    export SMART_TEXTBOOK_DATASET_ROOT=/opt/textbook/data/mnist
    export SMART_TEXTBOOK_RUNTIME_HELPERS_ROOT=/opt/textbook/reference-labs
    export ASCEND_PROCESS_LOG_PATH="$HOME/.ascend"
fi
