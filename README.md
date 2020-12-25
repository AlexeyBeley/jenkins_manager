# jenkins_manager
Class to control several aspects of Jenkins jobs' lifecycle:

## Jobs execution flow:
```
. Trigger asynchronously N jobs.
. Report any failures.
```

## Job creation and dumping:
```
. Dump jobs' configuration
. Create jobs from dumps.
```

## Example
Several use case examples in `tests` dir.
