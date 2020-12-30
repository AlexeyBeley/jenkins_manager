# jenkins_manager
Class to control several aspects of Jenkins jobs' lifecycle:

## Jobs execution flow:
```
. Trigger asynchronously N jobs.
. Uses protection mechanism (uid_parameter_name) - to identify the build. It overcomes Jenkins' bug of "loosing" queue_item build
  This is a regular parameter. But it used for system functionality. No need to be set explicitly. The code handles it. 
. Report any failures.
```

## Job creation and dumping:
```
. Dump jobs' configuration
. Create jobs from dumps.
```

## Jobs cleanup:
```
. Various methods to clean faulty jobs
```

## Example
Several use case examples in `tests` dir.
