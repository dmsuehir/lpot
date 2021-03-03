#!/bin/bash
set -x

function main {

  init_params "$@"
  run_tuning

}

# init params
function init_params {

  for var in "$@"
  do
    case $var in
      --topology=*)
          topology=$(echo $var |cut -f2 -d=)
      ;;
      --dataset_location=*)
          dataset_location=$(echo $var |cut -f2 -d=)
      ;;
      --input_model=*)
          input_model=$(echo $var |cut -f2 -d=)
      ;;
      --output_model=*)
          output_model=$(echo $var |cut -f2 -d=)
      ;;
      *)
          echo "Error: No such parameter: ${var}"
          exit 1
      ;;
    esac
  done

}

# run_tuning
function run_tuning {
    inference_script="inference.py"
    config_yaml="wide_deep_large_ds.yaml"
    if [[ ! -f "$inference_script" ]]; then
        if [[ ! -z "${LPOT_SOURCE_DIR}" ]]; then
            inference_script="${LPOT_SOURCE_DIR}/examples/tensorflow/recommendation/wide_deep_large_ds/inference.py"
            config_yaml="${LPOT_SOURCE_DIR}/examples/tensorflow/recommendation/wide_deep_large_ds/wide_deep_large_ds.yaml"
        fi
    fi

    echo "Config: ${config_yaml}"
    echo "Input model: ${input_model}"
    echo "Output model: ${output_model}"
    echo "Running script: ${inference_script}"

    python $inference_script \
            --input_graph ${input_model} \
            --evaluation_data_location ${dataset_location}/eval.csv \
            --calibration_data_location ${dataset_location}/train.csv \
            --accuracy_only \
            --batch_size 1000 \
            --output_graph ${output_model} \
            --config ${config_yaml} \
            --tune
}

main "$@"
