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
    python inference.py \
            --input_graph ${input_model} \
            --evaluation_data_location ${dataset_location}/eval.csv \
            --calibration_data_location ${dataset_location}/train.csv \
            --accuracy_only \
            --batch_size 1000 \
            --output_graph ${output_model} \
            --config wide_deep_large_ds.yaml \
            --tune
}

main "$@"
